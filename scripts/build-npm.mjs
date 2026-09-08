#!/usr/bin/env node
/**
 * Assemble the npm packages from the binaries CI built.
 *
 *   node scripts/build-npm.mjs --artifacts dist/artifacts --out dist/npm
 *
 * Input is one directory per platform, each holding the PyInstaller output for
 * that platform:
 *
 *   dist/artifacts/
 *     autordp-win32-x64/autordp.exe
 *     autordp-linux-x64/autordp
 *     autordp-darwin-arm64/autordp
 *     ...
 *
 * Output is a publishable package per platform plus the launcher, all stamped
 * with the version read from src/autordp/__init__.py -- which is the single place a
 * release is declared. Nothing here reads a version from a package.json, so the
 * Python package and the npm packages can never disagree about what they are.
 *
 * Deliberately dependency-free: this runs in CI before any npm install has
 * happened, and adding a dependency here would mean a lockfile and an install
 * step in front of the step that builds the thing being installed.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

// platform key -> the npm `os`/`cpu` values and a human name for the README.
const TARGETS = {
  "win32-x64": { os: "win32", cpu: "x64", label: "Windows x64" },
  "linux-x64": { os: "linux", cpu: "x64", label: "Linux x64" },
  "linux-arm64": { os: "linux", cpu: "arm64", label: "Linux arm64" },
  "darwin-arm64": { os: "darwin", cpu: "arm64", label: "macOS Apple silicon" },
};

const SCOPE = "@ayushshukla8920";
const NAME = "autordp";

function main() {
  const options = parseArgs(process.argv.slice(2));
  const version = readVersion();
  const launcher = JSON.parse(
    fs.readFileSync(path.join(ROOT, "npm", NAME, "package.json"), "utf8")
  );

  fs.rmSync(options.out, { recursive: true, force: true });
  fs.mkdirSync(options.out, { recursive: true });

  const built = [];
  for (const [key, target] of Object.entries(TARGETS)) {
    const binary = findBinary(options.artifacts, key);
    if (!binary) {
      // A missing platform is a warning, not an error: it lets a release go
      // out when one runner in the matrix is broken, and the launcher already
      // tells anyone on that platform exactly what happened.
      console.warn(`  ! skipping ${key}: no binary under ${options.artifacts}`);
      continue;
    }
    buildPlatformPackage({ key, target, binary, version, out: options.out });
    built.push(key);
  }

  if (built.length === 0) {
    fail(`No platform binaries found under ${options.artifacts}`);
  }

  buildLauncherPackage({ launcher, version, built, out: options.out });

  console.log(`\nBuilt ${built.length + 1} package(s) at version ${version}:`);
  console.log(`  ${SCOPE}/${NAME}`);
  for (const key of built) console.log(`  ${SCOPE}/${NAME}-${key}`);
  if (built.length !== Object.keys(TARGETS).length) {
    console.log(
      `\nNote: ${Object.keys(TARGETS).length - built.length} platform(s) ` +
        `missing. The launcher lists only what was built.`
    );
  }
}

// --------------------------------------------------------------- the packages

function buildPlatformPackage({ key, target, binary, version, out }) {
  const dir = path.join(out, `${NAME}-${key}`);
  fs.mkdirSync(path.join(dir, "bin"), { recursive: true });

  const name = target.os === "win32" ? "autordp.exe" : "autordp";
  const destination = path.join(dir, "bin", name);
  fs.copyFileSync(binary, destination);
  // npm packs whatever mode the file has. Without this, a binary built on a
  // runner that lost the bit -- or one copied out of a zip artifact, which has
  // no modes at all -- installs unrunnable.
  if (target.os !== "win32") fs.chmodSync(destination, 0o755);

  const size = (fs.statSync(destination).size / 1024 / 1024).toFixed(1);
  console.log(`  ${key.padEnd(14)} ${size} MB`);

  write(path.join(dir, "package.json"), {
    name: `${SCOPE}/${NAME}-${key}`,
    version,
    description: `autordp binary for ${target.label}.`,
    license: "MIT",
    repository: {
      type: "git",
      url: "git+https://github.com/ayushshukla8920/autoRDP.git",
    },
    // npm reads these before unpacking and skips the package entirely when
    // they do not match, which is the whole mechanism: one install, one binary.
    os: [target.os],
    cpu: [target.cpu],
    files: [`bin/${name}`],
    // No `provenance` here: it is only valid in a supported CI, and setting
    // it in the manifest makes a local `npm publish` fail outright.
    // release.yml passes --provenance on the command line instead.
    publishConfig: { access: "public" },
  });

  fs.writeFileSync(
    path.join(dir, "README.md"),
    `# ${SCOPE}/${NAME}-${key}\n\n` +
      `The autordp binary for ${target.label}.\n\n` +
      `Not meant to be installed directly. Install \`${SCOPE}/${NAME}\`, ` +
      `which depends on this one and picks the right build for the machine ` +
      `it lands on.\n`
  );
}

function buildLauncherPackage({ launcher, version, built, out }) {
  const dir = path.join(out, NAME);
  fs.mkdirSync(path.join(dir, "bin"), { recursive: true });

  for (const file of ["bin/autordp.js", "install.js"]) {
    fs.copyFileSync(path.join(ROOT, "npm", NAME, file), path.join(dir, file));
  }
  fs.copyFileSync(
    path.join(ROOT, "npm", NAME, "README.md"),
    path.join(dir, "README.md")
  );

  // Only platforms that were actually built are declared. Listing one that was
  // not published would make `npm install` fail outright on every platform,
  // because a missing optionalDependency version is still a resolution error.
  const optional = {};
  for (const key of built.sort()) optional[`${SCOPE}/${NAME}-${key}`] = version;

  write(path.join(dir, "package.json"), {
    ...launcher,
    version,
    optionalDependencies: optional,
  });
}

// -------------------------------------------------------------------- helpers

function readVersion() {
  const source = fs.readFileSync(
    path.join(ROOT, "src", "autordp", "__init__.py"),
    "utf8"
  );
  const match = source.match(/^__version__\s*=\s*["']([^"']+)["']/m);
  if (!match) fail("No __version__ found in src/autordp/__init__.py");
  return match[1];
}

function findBinary(artifacts, key) {
  // Tolerant of how the artifact was uploaded: the binary may sit in a
  // directory named for the platform, or be the only file in one.
  const candidates = [
    path.join(artifacts, `${NAME}-${key}`, "autordp.exe"),
    path.join(artifacts, `${NAME}-${key}`, "autordp"),
    path.join(artifacts, key, "autordp.exe"),
    path.join(artifacts, key, "autordp"),
  ];
  return candidates.find((candidate) => fs.existsSync(candidate)) || null;
}

function write(file, object) {
  if (object.name) validateName(object.name);
  for (const dep of Object.keys(object.optionalDependencies || {})) {
    validateName(dep);
  }
  fs.writeFileSync(file, JSON.stringify(object, null, 2) + "\n");
}

/**
 * npm refuses a new package whose name contains a capital letter, and it only
 * says so at `npm publish` -- after the binaries are built and, in CI, after
 * some of the platform packages may already be live. The repository is called
 * autoRDP, so a careless search-and-replace over the GitHub URLs can reach the
 * name field too. Failing here costs a second; failing at publish costs a
 * half-published release.
 */
function validateName(name) {
  if (name !== name.toLowerCase()) {
    fail(`package name "${name}" has capital letters; npm will reject it`);
  }
}

function parseArgs(argv) {
  const options = { artifacts: "dist/artifacts", out: "dist/npm" };
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index];
    if (flag === "--artifacts" || flag === "--out") {
      const value = argv[index + 1];
      if (!value) fail(`${flag} needs a value`);
      options[flag.slice(2)] = path.resolve(ROOT, value);
      index += 1;
    } else if (flag === "--help" || flag === "-h") {
      console.log(
        "usage: node scripts/build-npm.mjs [--artifacts DIR] [--out DIR]"
      );
      process.exit(0);
    } else {
      fail(`Unknown argument ${flag}`);
    }
  }
  options.artifacts = path.resolve(ROOT, options.artifacts);
  options.out = path.resolve(ROOT, options.out);
  if (!fs.existsSync(options.artifacts)) {
    fail(`No such artifacts directory: ${options.artifacts}`);
  }
  return options;
}

function fail(message) {
  console.error(`build-npm: ${message}`);
  process.exit(1);
}

main();
