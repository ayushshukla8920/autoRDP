#!/usr/bin/env node
/**
 * Cut a release.
 *
 *   node scripts/release.mjs                 release the current __version__
 *   node scripts/release.mjs --version 0.2.0 bump first, then release
 *   node scripts/release.mjs --dry-run       check everything, change nothing
 *
 * This does **not** publish. It cannot: PyInstaller has no cross-compilation,
 * so a macOS binary needs a macOS machine and an arm64 Linux binary needs
 * arm64 Linux. One workstation can only ever produce one of the packages, and
 * publishing a partial release would burn the version number for the rest --
 * npm never lets a version be reused, even after `npm unpublish`.
 *
 * So the split is:
 *
 *   here   verify, build and smoke-test locally, then tag and push
 *   CI     build every platform and publish  (.github/workflows/release.yml)
 *
 * The local build is a gate, not an artifact. Pushing a tag starts a job that
 * ends in an irreversible publish, and the failures worth catching -- a broken
 * spec, a missing hidden import, a hollow binary -- fail identically here, in
 * ninety seconds, before anything is public.
 *
 * Dependency-free on purpose: this runs before any npm install has happened.
 */

import { execFileSync, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const VERSION_FILE = path.join(ROOT, "src", "autordp", "__init__.py");
const SCOPE = "@ayushshukla8920";
const NAME = "autordp";
const PLATFORMS = [
  "win32-x64",
  "linux-x64",
  "linux-arm64",
  "darwin-arm64",
];

const options = parseArgs(process.argv.slice(2));
let step = 0;

// --------------------------------------------------------------------- steps

async function main() {
  heading(`Releasing ${SCOPE}/${NAME}`);

  const version = options.version ?? readVersion();
  if (options.version) bumpVersion(options.version);
  info(`version ${version}`);

  checkGit();
  checkRegistry(version);
  const binary = options.skipBuild ? null : buildAndSmokeTest();
  if (binary) checkPackaging(version, binary);

  if (options.dryRun) {
    ok("dry run: everything that can be checked locally passes");
    note("re-run without --dry-run to tag and push");
    return;
  }

  await confirm(version);
  tagAndPush(version);
  finish(version);
}

// ------------------------------------------------------------------ preflight

function checkGit() {
  begin("Checking the working tree");

  const dirty = git("status", "--porcelain").trim();
  if (dirty && !options.version) {
    // With --version we are about to commit the bump ourselves, so a dirty
    // tree is expected; without it, uncommitted work means the tag would point
    // at something that is not what was tested.
    fail(
      "the working tree has uncommitted changes:\n" +
        dirty
          .split("\n")
          .map((l) => "        " + l)
          .join("\n") +
        "\n\n        Commit or stash them: the tag must point at the code that was built."
    );
  }

  const branch = git("rev-parse", "--abbrev-ref", "HEAD").trim();
  if (branch !== "main") {
    warn(`on branch "${branch}", not main`);
  }

  try {
    git("remote", "get-url", "origin");
  } catch {
    fail("no `origin` remote, so there is nowhere to push the tag.");
  }

  ok(`branch ${branch}, tree clean`);
}

function checkRegistry(version) {
  begin("Checking npm");

  const who = run("npm", ["whoami"], { allowFail: true });
  if (who.status !== 0) {
    fail("not logged in to npm. Run `npm login` first.\n" +
         "        (CI publishes with NPM_TOKEN, but this check needs you.)");
  }
  const user = who.stdout.trim();
  const owner = SCOPE.slice(1);
  if (user !== owner) {
    warn(`logged in as "${user}" but the scope is ${SCOPE}; ` +
         `publishing needs membership of that scope`);
  } else {
    ok(`npm user ${user}`);
  }

  // npm never allows a version to be republished, even after unpublishing, so
  // a collision has to be caught before the tag goes anywhere near CI.
  const taken = [];
  for (const suffix of ["", ...PLATFORMS.map((p) => `-${p}`)]) {
    const pkg = `${SCOPE}/${NAME}${suffix}`;
    const probe = run("npm", ["view", `${pkg}@${version}`, "version"], {
      allowFail: true,
    });
    if (probe.status === 0 && probe.stdout.trim()) taken.push(pkg);
  }
  if (taken.length) {
    fail(
      `version ${version} is already published for:\n` +
        taken.map((p) => `        ${p}`).join("\n") +
        "\n\n        npm never allows a version to be reused. Bump it:\n" +
        `        node scripts/release.mjs --version ${nextPatch(version)}`
    );
  }
  ok(`${version} is free on the registry`);

  const tag = `v${version}`;
  if (git("tag", "--list", tag).trim()) {
    fail(`tag ${tag} already exists locally. Delete it with ` +
         `\`git tag -d ${tag}\`, or bump the version.`);
  }
}

// ---------------------------------------------------------------------- build

function buildAndSmokeTest() {
  begin("Building for this platform");

  const python = findPython();
  info(`python: ${python}`);
  const build = run(python, [
    "-m", "PyInstaller", "autordp.spec", "--noconfirm", "--log-level", "WARN",
  ], {
    allowFail: true,
    quiet: false,
    // Cryptodome's self-test suite emits a wall of UserWarnings merely from
    // being imported during analysis. None of it is actionable.
    env: { PYTHONWARNINGS: "ignore" },
  });
  if (build.status !== 0) {
    fail(`PyInstaller exited ${build.status}. The tag was not created.\n\n` +
         tail(build.stderr));
  }

  const binary = path.join(
    ROOT, "dist", process.platform === "win32" ? "autordp.exe" : "autordp");
  if (!fs.existsSync(binary)) fail(`expected ${binary}, which is not there.`);
  const mb = (fs.statSync(binary).size / 1024 / 1024).toFixed(1);
  ok(`dist/${path.basename(binary)}  (${mb} MB)`);

  begin("Smoke-testing the binary");
  const version = run(binary, ["--version"], { allowFail: true });
  if (version.status !== 0) {
    fail(`the binary will not even print its version:\n${version.stderr}`);
  }
  ok(version.stdout.trim());

  // The assertion that matters. aardwolf reaches its 400-odd keyboard layouts
  // through importlib, which PyInstaller's static analysis cannot see, so a
  // build missing them starts fine, connects fine, and dies on the first
  // keystroke -- possibly twenty minutes into a run. `doctor` loads a layout
  // and resolves a scancode through it, which turns that into a one-second
  // answer here rather than a published-and-broken binary.
  const report = run(binary, ["doctor", "--json", "--no-network", "--no-input"],
                     { allowFail: true });
  let checks;
  try {
    checks = Object.fromEntries(
      JSON.parse(report.stdout).checks.map((c) => [c.name, c]));
  } catch {
    fail(`doctor produced no usable JSON:\n${report.stdout || report.stderr}`);
  }
  for (const name of ["rdp stack", "keyboard layout"]) {
    const check = checks[name];
    if (!check) fail(`doctor did not report a "${name}" check.`);
    if (check.status !== "ok") {
      fail(`the build is hollow -- ${name}: ${check.detail}\n` +
           "        Do not release this. Check autordp.spec's hidden imports.");
    }
    ok(`${name}: ${check.detail}`);
  }
  return binary;
}

function checkPackaging(version, binary) {
  begin("Checking the npm assembly");

  // Assembled for this platform only, purely to prove build-npm.mjs runs and
  // produces manifests npm will accept. CI does the real five-platform build.
  const key = `${process.platform}-${process.arch}`;
  const stage = path.join(ROOT, "dist", "artifacts", `${NAME}-${key}`);
  fs.rmSync(path.join(ROOT, "dist", "artifacts"), { recursive: true, force: true });
  fs.mkdirSync(stage, { recursive: true });
  fs.copyFileSync(binary, path.join(stage, path.basename(binary)));

  const assemble = run("node", [
    path.join("scripts", "build-npm.mjs"),
    "--artifacts", "dist/artifacts", "--out", "dist/npm",
  ], { allowFail: true });
  if (assemble.status !== 0) {
    fail(`build-npm.mjs exited ${assemble.status}:\n${assemble.stderr}`);
  }

  const manifest = JSON.parse(fs.readFileSync(
    path.join(ROOT, "dist", "npm", NAME, "package.json"), "utf8"));
  if (manifest.version !== version) {
    fail(`build-npm.mjs stamped ${manifest.version}, expected ${version}. ` +
         "Something reads the version from somewhere other than " +
         "src/autordp/__init__.py.");
  }
  ok(`manifests stamped ${version}`);
  note(`this machine can only build ${key}; CI builds the other ${PLATFORMS.length - 1}`);
}

// ----------------------------------------------------------------- tag & push

async function confirm(version) {
  const tag = `v${version}`;
  console.log();
  console.log(`  This will push ${bold(tag)}, which starts a GitHub Actions run`);
  console.log(`  that builds ${PLATFORMS.length} binaries and ${bold("publishes them to npm")}.`);
  console.log(`  Publishing cannot be undone: npm never allows ${version} to be reused.`);
  console.log();

  if (options.yes) return;
  if (!process.stdin.isTTY) {
    fail("not a terminal, so there is nobody to confirm. Pass --yes if you " +
         "are sure, or --dry-run to check without releasing.");
  }
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  const answer = await new Promise((resolve) =>
    rl.question(`  Type ${bold(tag)} to continue: `, resolve));
  rl.close();
  if (answer.trim() !== tag) fail("cancelled.");
}

function tagAndPush(version) {
  const tag = `v${version}`;

  if (options.version) {
    begin("Committing the version bump");
    git("add", path.relative(ROOT, VERSION_FILE).split(path.sep).join("/"));
    git("commit", "-m", `Release ${tag}`);
    ok(`committed "Release ${tag}"`);
  }

  begin("Tagging and pushing");
  git("tag", "-a", tag, "-m", `autordp ${version}`);
  ok(`created ${tag}`);

  const branch = git("rev-parse", "--abbrev-ref", "HEAD").trim();
  // Branch first, then the tag. A tag pushed to a remote that does not yet
  // have the commit it points at leaves the workflow checking out a commit
  // nobody can see.
  git("push", "origin", branch);
  ok(`pushed ${branch}`);
  git("push", "origin", tag);
  ok(`pushed ${tag}`);
}

function finish(version) {
  let slug = "ayushshukla8920/autoRDP";
  try {
    const url = git("remote", "get-url", "origin").trim();
    const match = url.match(/[:/]([^/:]+\/[^/]+?)(?:\.git)?$/);
    if (match) slug = match[1];
  } catch {
    /* keep the default */
  }

  console.log();
  console.log(bold("  Released. CI is building now."));
  console.log();
  console.log(`    https://github.com/${slug}/actions`);
  console.log();
  console.log("  When it finishes:");
  console.log(`    npm install -g ${SCOPE}/${NAME}@${version}`);
  console.log();
  note("If the run fails on the publish step, NPM_TOKEN is probably missing");
  note(`from ${bold("Settings -> Secrets and variables -> Actions")}.`);
}

// -------------------------------------------------------------------- version

function readVersion() {
  const source = fs.readFileSync(VERSION_FILE, "utf8");
  const match = source.match(/^__version__\s*=\s*["']([^"']+)["']/m);
  if (!match) fail(`no __version__ in ${VERSION_FILE}`);
  return match[1];
}

function bumpVersion(version) {
  if (!/^\d+\.\d+\.\d+([-+].+)?$/.test(version)) {
    fail(`"${version}" is not a version. Use MAJOR.MINOR.PATCH.`);
  }
  const source = fs.readFileSync(VERSION_FILE, "utf8");
  const updated = source.replace(
    /^(__version__\s*=\s*)["'][^"']+["']/m, `$1"${version}"`);
  if (updated === source) fail(`could not rewrite __version__ in ${VERSION_FILE}`);
  fs.writeFileSync(VERSION_FILE, updated);
  ok(`bumped src/autordp/__init__.py to ${version}`);
}

function nextPatch(version) {
  const [major, minor, patch] = version.split(".").map(Number);
  return Number.isFinite(patch) ? `${major}.${minor}.${patch + 1}` : "0.1.1";
}

// -------------------------------------------------------------------- helpers

function findPython() {
  const venv = process.platform === "win32"
    ? path.join(ROOT, ".venv", "Scripts", "python.exe")
    : path.join(ROOT, ".venv", "bin", "python");
  if (fs.existsSync(venv)) return venv;
  for (const candidate of ["python3", "python", "py"]) {
    const probe = run(candidate, ["--version"], { allowFail: true });
    if (probe.status === 0) return candidate;
  }
  fail("no Python found. Create the virtual environment first:\n" +
       "        py -3.13 -m venv .venv && .venv\\Scripts\\pip install -r requirements.txt \"pyinstaller>=6.3\"");
}

function run(command, args, { allowFail = false, quiet = true, env } = {}) {
  // The shell is needed for exactly one thing on Windows: `npm` and `node` are
  // .cmd shims, and spawnSync cannot execute those directly -- it fails with
  // ENOENT. It must NOT be used for a real executable path, because cmd.exe
  // splits an unquoted command on spaces, so a venv under "C:\Users\Ada
  // Lovelace\..." becomes an attempt to run "C:\Users\Ada". That failure looks
  // exactly like a build error, which is a miserable thing to debug.
  const shell = process.platform === "win32"
    && !path.isAbsolute(command)
    && !command.toLowerCase().endsWith(".exe");

  const result = spawnSync(command, args, {
    cwd: ROOT,
    encoding: "utf8",
    stdio: quiet ? "pipe" : ["ignore", "inherit", "pipe"],
    shell,
    env: env ? { ...process.env, ...env } : process.env,
  });
  if (result.error && !allowFail) fail(`${command}: ${result.error.message}`);
  if (result.status !== 0 && !allowFail) {
    fail(`${command} exited ${result.status}\n${result.stderr || ""}`);
  }
  return { status: result.status ?? 1, stdout: result.stdout ?? "", stderr: result.stderr ?? "" };
}

/** The last few meaningful lines of a failed command, for the error message. */
function tail(text, lines = 12) {
  return (text || "")
    .split("\n")
    .filter((line) => line.trim() && !/^\s*(warnings\.warn|[A-Za-z]:\\.*UserWarning)/.test(line))
    .slice(-lines)
    .map((line) => "        " + line.trimEnd())
    .join("\n");
}

function git(...args) {
  return execFileSync("git", args, { cwd: ROOT, encoding: "utf8" });
}

function parseArgs(argv) {
  const options = { dryRun: false, skipBuild: false, yes: false, version: null };
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    if (flag === "--dry-run") options.dryRun = true;
    else if (flag === "--skip-build") options.skipBuild = true;
    else if (flag === "--yes" || flag === "-y") options.yes = true;
    else if (flag === "--version") {
      options.version = argv[++i];
      if (!options.version) fail("--version needs a value, e.g. --version 0.2.0");
    } else if (flag === "--help" || flag === "-h") {
      console.log(`usage: node scripts/release.mjs [options]

  --version X.Y.Z   bump src/autordp/__init__.py first
  --dry-run         run every check, but do not tag or push
  --skip-build      trust the binary already in dist/ (not recommended)
  --yes, -y         skip the confirmation prompt
`);
      process.exit(0);
    } else fail(`unknown argument ${flag}`);
  }
  return options;
}

// ------------------------------------------------------------------ reporting

const colour = process.stdout.isTTY && !process.env.NO_COLOR;
const bold = (t) => (colour ? `[1m${t}[0m` : t);
const dim = (t) => (colour ? `[90m${t}[0m` : t);
const green = (t) => (colour ? `[32m${t}[0m` : t);
const yellow = (t) => (colour ? `[33m${t}[0m` : t);
const red = (t) => (colour ? `[31m${t}[0m` : t);

function heading(text) {
  console.log();
  console.log(bold(text));
  console.log(dim("-".repeat(Math.max(text.length, 40))));
}
function begin(text) {
  step += 1;
  console.log(`${bold(`[${step}]`)} ${text}`);
}
const ok = (t) => console.log(`    ${green("+")} ${t}`);
const info = (t) => console.log(`    ${dim(t)}`);
const note = (t) => console.log(`    ${dim(t)}`);
const warn = (t) => console.log(`    ${yellow("!")} ${t}`);

function fail(message) {
  console.error();
  console.error(`${red("RELEASE FAILED:")} ${message}`);
  console.error();
  process.exit(1);
}

main().catch((error) => fail(error.stack || String(error)));
