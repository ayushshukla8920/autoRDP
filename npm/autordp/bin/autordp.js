#!/usr/bin/env node
"use strict";

/**
 * Launcher for the platform binary.
 *
 * The real program is a PyInstaller build of the Python package, shipped in one
 * of several `@ayushshukla8920/autordp-<platform>-<arch>` packages. npm installs
 * only the one matching this machine, because each declares `os` and `cpu` and
 * they are listed as optionalDependencies -- the rest fail their platform check
 * and are skipped silently. This file finds whichever one landed and hands over
 * to it.
 *
 * `require.resolve` rather than a constructed path: it walks the same lookup
 * npm itself uses, so hoisting, nested node_modules, pnpm's symlink store and a
 * yarn workspace all resolve correctly, none of which a hand-built
 * `../../autordp-win32-x64/bin/...` would.
 */

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");

const SCOPE = "@ayushshukla8920";
const PACKAGE = "autordp";

// The platforms a binary is actually built for. Listed explicitly so that an
// unsupported one gets a useful message rather than a module-not-found trace.
const BUILT = [
  "win32-x64",
  "linux-x64",
  "linux-arm64",
  "darwin-arm64",
];

// Where a platform with no native build can borrow one that will still run.
// Windows on ARM executes x64 binaries under emulation, so a native arm64 Node
// -- which reports arch "arm64" while an x64 Node on the same machine reports
// "x64" -- should use the x64 build rather than refuse to start.
const EMULATED = { "win32-arm64": "win32-x64" };

function platformKey() {
  const key = `${process.platform}-${process.arch}`;
  return EMULATED[key] || key;
}

function binaryName() {
  return process.platform === "win32" ? "autordp.exe" : "autordp";
}

/** @returns {{binary: string} | {error: string}} */
function locate() {
  const key = platformKey();
  const pkg = `${SCOPE}/${PACKAGE}-${key}`;
  let manifest;
  try {
    // Resolve the package's own manifest, not its main: these packages contain
    // only a binary and have no JavaScript entry point to load.
    manifest = require.resolve(`${pkg}/package.json`);
  } catch {
    return { error: missingMessage(key, pkg) };
  }
  const binary = manifest.replace(/package\.json$/, `bin/${binaryName()}`);
  if (!fs.existsSync(binary)) {
    return { error: `${pkg} is installed but ${binary} is missing.` };
  }
  return { binary };
}

function missingMessage(key, pkg) {
  if (!BUILT.includes(key)) {
    return (
      `autordp has no prebuilt binary for ${process.platform}-${process.arch}.\n` +
      `Built for: ${BUILT.join(", ")}.\n` +
      `You can still run it from source: pip install autordp`
    );
  }
  return (
    `The binary package ${pkg} is not installed.\n\n` +
    `Either your installer skipped optional dependencies:\n` +
    `  npm install ${SCOPE}/${PACKAGE} --force\n` +
    `  npm install ${pkg}\n` +
    `(--no-optional and ignore-optional make the binary unfetchable, so that\n` +
    ` setting has to be relaxed.)\n\n` +
    `...or no binary was published for this platform in this release. Not\n` +
    `every platform has a prebuilt wheel of the underlying RDP library, so a\n` +
    `release can ship without one. Run it from source instead:\n` +
    `  pip install autordp`
  );
}

function main() {
  const found = locate();
  if (found.error) {
    process.stderr.write(found.error + "\n");
    process.exit(1);
  }

  // spawnSync with inherited stdio, rather than exec-and-replace, because Node
  // has no execve. The cost is one extra process in the tree; the benefit is
  // that this works identically on Windows, where there is no exec at all.
  //
  // Signals are inherited along with the terminal, so Ctrl+C reaches the child
  // directly -- which matters here, because Ctrl+C is the emergency stop.
  const result = spawnSync(found.binary, process.argv.slice(2), {
    stdio: "inherit",
    windowsHide: false,
  });

  if (result.error) {
    if (result.error.code === "EACCES") {
      process.stderr.write(
        `${found.binary} is not executable.\n` +
          `Fix it with: chmod +x "${found.binary}"\n`
      );
      process.exit(1);
    }
    process.stderr.write(`Failed to start autordp: ${result.error.message}\n`);
    process.exit(1);
  }

  // A child killed by a signal has a null status. Reporting 128+signal is the
  // shell convention, and it keeps `autordp ... ; echo $?` meaningful after a
  // Ctrl+C -- which for this tool is a normal way to finish.
  if (result.signal) {
    const signals = { SIGINT: 2, SIGTERM: 15, SIGHUP: 1, SIGQUIT: 3 };
    process.exit(128 + (signals[result.signal] || 0));
  }
  process.exit(result.status === null ? 1 : result.status);
}

main();
