"use strict";

/**
 * postinstall: make sure the platform binary arrived, and that it is runnable.
 *
 * This never fails the install. It cannot: `npm install --ignore-scripts` skips
 * this file entirely, so anything essential here would be essential and absent.
 * bin/autordp.js re-checks and prints the same guidance at run time. This is
 * only here to say something at the moment the user is watching the install,
 * rather than the next morning when they first run the command.
 *
 * The one thing it does fix is the executable bit. npm preserves file modes
 * from the tarball, but some registries, mirrors and older pnpm versions do
 * not, and a binary without +x fails with EACCES for no visible reason.
 */

const fs = require("node:fs");

const SCOPE = "@ayushshukla8920";
const PACKAGE = "autordp";

function main() {
  const key = `${process.platform}-${process.arch}`;
  const pkg = `${SCOPE}/${PACKAGE}-${key}`;
  const name = process.platform === "win32" ? "autordp.exe" : "autordp";

  let binary;
  try {
    binary = require
      .resolve(`${pkg}/package.json`)
      .replace(/package\.json$/, `bin/${name}`);
  } catch {
    warn(
      `no prebuilt binary for ${key} was installed.\n` +
        `  Run \`npx ${SCOPE}/${PACKAGE} --version\` for the full explanation.`
    );
    return;
  }

  if (!fs.existsSync(binary)) {
    warn(`${pkg} is installed but ${name} is missing from it.`);
    return;
  }

  if (process.platform !== "win32") {
    try {
      fs.chmodSync(binary, 0o755);
    } catch (error) {
      warn(`could not mark ${binary} executable: ${error.message}`);
    }
  }
}

function warn(message) {
  process.stderr.write(`\n${SCOPE}/${PACKAGE}: ${message}\n\n`);
}

main();
