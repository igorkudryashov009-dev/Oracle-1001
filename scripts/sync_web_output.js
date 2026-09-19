#!/usr/bin/env node
/**
 * sync_web_output.js — SHA-256 parity + atomic mirror web/ ↔ output/
 *
 * SoT: web/ (authoring). Destination: output/js|css (+ dual CSS into output/js).
 * Hot paths (always reported first): top10_sheet.js, arctic_sheet.js,
 * oracle_sheet.js, sentinel_hud.css.
 *
 * Usage:
 *   node scripts/sync_web_output.js
 *   node scripts/sync_web_output.js --check   # verify only (exit 1 on drift)
 *   node scripts/sync_web_output.js --watch   # sync now + watch hot files
 *   node scripts/sync_web_output.js --json
 */

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");

/** Priority HUD surfaces — mirrored immediately on any drift. */
const HOT_NAMES = new Set([
  "top10_sheet.js",
  "arctic_sheet.js",
  "oracle_sheet.js",
  "sentinel_hud.css",
]);

/**
 * @typedef {{ src: string, dst: string, kind: string, hot: boolean }} Pair
 */

/**
 * @param {string} rel
 */
function abs(rel) {
  return path.join(ROOT, rel);
}

/**
 * Build managed pairs (mirrors services/web_assets_sync.py).
 * @returns {Pair[]}
 */
function buildPairs() {
  /** @type {Pair[]} */
  const pairs = [];
  const webJs = abs("web/js");
  const webCss = abs("web/css");
  const webRoot = abs("web");
  const outJs = abs("output/js");
  const outCss = abs("output/css");

  if (fs.existsSync(webJs)) {
    for (const name of fs.readdirSync(webJs).sort()) {
      if (!name.endsWith(".js")) continue;
      pairs.push({
        src: path.join(webJs, name),
        dst: path.join(outJs, name),
        kind: "js",
        hot: HOT_NAMES.has(name),
      });
    }
  }

  if (fs.existsSync(webCss)) {
    for (const name of fs.readdirSync(webCss).sort()) {
      if (!name.endsWith(".css")) continue;
      const hot = HOT_NAMES.has(name);
      pairs.push({
        src: path.join(webCss, name),
        dst: path.join(outCss, name),
        kind: "css",
        hot,
      });
      pairs.push({
        src: path.join(webCss, name),
        dst: path.join(outJs, name),
        kind: "css_dual",
        hot,
      });
    }
  }

  if (fs.existsSync(webRoot)) {
    for (const name of fs.readdirSync(webRoot).sort()) {
      const src = path.join(webRoot, name);
      if (!fs.statSync(src).isFile()) continue;
      if (name.endsWith(".js")) {
        const jsSibling = path.join(webJs, name);
        // If web/js/<name> exists, it is the SoT for output/js/<name>.
        // Root web/<name>.js must not clobber it (oracle_sheet re-export case).
        if (!fs.existsSync(jsSibling)) {
          pairs.push({
            src,
            dst: path.join(outJs, name),
            kind: "js_root",
            hot: HOT_NAMES.has(name),
          });
        }
        // Architect path: web/oracle_sheet.js → output/oracle_sheet.js
        if (name === "oracle_sheet.js") {
          pairs.push({
            src,
            dst: abs("output/oracle_sheet.js"),
            kind: "js_root_mirror",
            hot: true,
          });
        }
      } else if (name.endsWith(".css")) {
        pairs.push({
          src,
          dst: path.join(outJs, name),
          kind: "css_dual",
          hot: HOT_NAMES.has(name),
        });
        pairs.push({
          src,
          dst: path.join(outCss, name),
          kind: "css",
          hot: HOT_NAMES.has(name),
        });
      }
    }
  }

  return pairs;
}

/**
 * @param {string} filePath
 */
function sha256File(filePath) {
  const h = crypto.createHash("sha256");
  h.update(fs.readFileSync(filePath));
  return h.digest("hex");
}

/**
 * Atomic write (temp + rename).
 * @param {string} dst
 * @param {Buffer} data
 */
function atomicWrite(dst, data) {
  fs.mkdirSync(path.dirname(dst), { recursive: true });
  const tmp = path.join(
    path.dirname(dst),
    `.${path.basename(dst)}.${process.pid}.${Date.now()}.tmp`,
  );
  try {
    fs.writeFileSync(tmp, data);
    fs.renameSync(tmp, dst);
  } catch (err) {
    try {
      if (fs.existsSync(tmp)) fs.unlinkSync(tmp);
    } catch {
      /* ignore */
    }
    throw err;
  }
}

/**
 * @param {string} p
 */
function rel(p) {
  return path.relative(ROOT, p).split(path.sep).join("/");
}

/**
 * @param {Pair} pair
 * @param {{ checkOnly?: boolean }} [opts]
 */
function syncPair(pair, opts = {}) {
  const { checkOnly = false } = opts;
  if (!fs.existsSync(pair.src)) {
    return {
      status: "MISSING_SRC",
      src: rel(pair.src),
      dst: rel(pair.dst),
      kind: pair.kind,
      hot: pair.hot,
      ok: false,
    };
  }
  const data = fs.readFileSync(pair.src);
  const srcSha = crypto.createHash("sha256").update(data).digest("hex");
  let dstSha = null;
  let status = "WRITE";
  let ok = false;

  if (fs.existsSync(pair.dst)) {
    dstSha = sha256File(pair.dst);
    if (dstSha === srcSha) {
      return {
        status: "MATCH",
        src: rel(pair.src),
        dst: rel(pair.dst),
        kind: pair.kind,
        hot: pair.hot,
        bytes: data.length,
        sha256: srcSha,
        ok: true,
      };
    }
    status = "DRIFT";
  } else {
    status = "MISSING_DST";
  }

  if (checkOnly) {
    return {
      status,
      src: rel(pair.src),
      dst: rel(pair.dst),
      kind: pair.kind,
      hot: pair.hot,
      bytes: data.length,
      src_sha256: srcSha,
      dst_sha256: dstSha,
      ok: false,
    };
  }

  atomicWrite(pair.dst, data);
  const verify = sha256File(pair.dst);
  ok = verify === srcSha;
  return {
    status: ok ? "SYNCED" : "VERIFY_FAIL",
    src: rel(pair.src),
    dst: rel(pair.dst),
    kind: pair.kind,
    hot: pair.hot,
    bytes: data.length,
    sha256: srcSha,
    ok,
  };
}

/**
 * @param {ReturnType<typeof syncPair>[]} rows
 * @param {{ json?: boolean }} opts
 */
function printReport(rows, opts = {}) {
  const hot = rows.filter((r) => r.hot);
  const rest = rows.filter((r) => !r.hot);
  const matched = rows.filter((r) => r.ok).length;
  const drifted = rows.filter((r) => !r.ok).length;
  const synced = rows.filter((r) => r.status === "SYNCED").length;

  if (opts.json) {
    console.log(
      JSON.stringify(
        {
          root: ROOT,
          checked: rows.length,
          matched,
          drifted,
          synced,
          identical: drifted === 0,
          hot,
          rows,
        },
        null,
        2,
      ),
    );
    return;
  }

  console.log("=== sync_web_output · web/ → output/ ===");
  console.log(`root: ${ROOT}`);
  console.log("");
  console.log("--- HOT (top10 / arctic / oracle / sentinel_hud.css) ---");
  for (const r of hot) {
    const tag = r.ok ? "OK  " : "FIX ";
    console.log(
      `  ${tag} ${r.status.padEnd(12)} ${r.src} → ${r.dst}` +
        (r.sha256 ? `  sha256=${r.sha256.slice(0, 12)}…` : ""),
    );
  }
  console.log("");
  console.log("--- ALL MANAGED PAIRS ---");
  for (const r of rest) {
    if (r.status === "MATCH") continue;
    const tag = r.ok ? "OK  " : "FIX ";
    console.log(`  ${tag} ${r.status.padEnd(12)} ${r.src} → ${r.dst}`);
  }
  const silentMatch = rest.filter((r) => r.status === "MATCH").length;
  console.log(`  … ${silentMatch} additional pairs already MATCH`);
  console.log("");
  console.log(
    `REPORT: checked=${rows.length} match=${matched} synced=${synced} drift=${drifted}`,
  );
  if (drifted === 0) {
    console.log("RESULT: web/ and output/ are 100% identical (managed pairs)");
  } else {
    console.log("RESULT: DRIFT remains — re-run without --check");
  }
}

/**
 * @param {string[]} argv
 */
function parseArgs(argv) {
  return {
    checkOnly: argv.includes("--check"),
    watch: argv.includes("--watch"),
    json: argv.includes("--json"),
  };
}

/**
 * Watch hot source files and re-sync instantly.
 * @param {Pair[]} pairs
 */
function watchHot(pairs) {
  const hotPairs = pairs.filter((p) => p.hot);
  const watched = new Map();
  console.log(`\n[WATCH] monitoring ${hotPairs.length} hot pair(s) — Ctrl+C to stop`);

  for (const pair of hotPairs) {
    const src = pair.src;
    if (watched.has(src)) continue;
    watched.set(src, true);
    try {
      fs.watch(src, { persistent: true }, () => {
        // Debounce burst events
        setTimeout(() => {
          if (!fs.existsSync(src)) return;
          console.log(`\n[WATCH] change detected: ${rel(src)}`);
          const related = pairs.filter((p) => p.src === src);
          const rows = related.map((p) => syncPair(p, { checkOnly: false }));
          printReport(
            [...rows, ...pairs.filter((p) => p.src !== src).map((p) => syncPair(p, { checkOnly: true }))],
            {},
          );
        }, 80);
      });
    } catch (err) {
      console.warn(`[WATCH] cannot watch ${rel(src)}:`, err.message || err);
    }
  }
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const pairs = buildPairs();
  if (!pairs.length) {
    console.error("No managed pairs found under web/");
    process.exit(2);
  }

  const rows = pairs.map((p) => syncPair(p, { checkOnly: opts.checkOnly }));
  printReport(rows, { json: opts.json });

  const identical = rows.every((r) => r.ok);
  if (!identical && opts.checkOnly) {
    process.exit(1);
  }
  if (!identical) {
    process.exit(1);
  }

  if (opts.watch) {
    watchHot(pairs);
    return;
  }
  process.exit(0);
}

main();
