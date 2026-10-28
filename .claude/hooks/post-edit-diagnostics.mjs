#!/usr/bin/env node
/**
 * PostToolUse hook — run diagnostics after file edits.
 *
 * Runs `tldr diagnostics` on the edited file to catch type errors
 * and lint issues immediately after Edit/Write. Shift-left feedback
 * before tests run.
 *
 * Falls through silently if tldr not installed or diagnostics unavailable.
 */
import { readFileSync, existsSync } from 'fs';
import { spawnSync } from 'child_process';
import { extname, basename, join } from 'path';
import { homedir } from 'os';

// Resolve tldr by absolute path first: a Claude Code process started before
// ~/.cargo/bin joined PATH can't find it by name, and the failure is silent.
const CARGO_TLDR = join(homedir(), '.cargo', 'bin', process.platform === 'win32' ? 'tldr.exe' : 'tldr');
const TLDR = existsSync(CARGO_TLDR) ? CARGO_TLDR : 'tldr';

const ENABLED_EXTENSIONS = new Set([
  '.py', '.pyx', '.pyi',                    // Python: ruff + pyright
  '.ts', '.tsx', '.js', '.jsx', '.mjs',      // JS/TS: eslint + tsc
  '.rs',                                      // Rust: cargo check + clippy
]);

// Errors first, then warnings — the listing is capped, so rank by urgency
const SEVERITY_RANK = { error: 0, warning: 1, info: 2, hint: 3 };

function main() {
  let data;
  try { data = JSON.parse(readFileSync(0, 'utf-8')); } catch { console.log('{}'); return; }

  const editTools = new Set(['Edit', 'Write', 'MultiEdit', 'Update']);
  if (!editTools.has(data.tool_name)) { console.log('{}'); return; }

  const filePath = (data.tool_input || {}).file_path || '';
  if (!filePath) { console.log('{}'); return; }

  const ext = extname(filePath);
  if (!ENABLED_EXTENSIONS.has(ext)) { console.log('{}'); return; }

  // Run tldr diagnostics on the file. spawnSync, not execSync: tldr exits 1
  // whenever it has findings, which is exactly when we want its stdout.
  const proc = spawnSync(TLDR, ['diagnostics', filePath, '--format', 'json'], {
    encoding: 'utf-8', timeout: 15000,
  });
  if (proc.error || !proc.stdout) { console.log('{}'); return; }

  let diag;
  try { diag = JSON.parse(proc.stdout); } catch { console.log('{}'); return; }

  // tldr emits { diagnostics: [...], summary: { errors, warnings, info, hints, total } }
  const findings = Array.isArray(diag.diagnostics) ? diag.diagnostics : [];
  const summary = diag.summary || {};
  const errors = summary.errors || 0;
  const warnings = summary.warnings || 0;
  const info = summary.info || 0;
  const hints = summary.hints || 0;
  const total = summary.total || findings.length;

  if (total === 0) { console.log('{}'); return; }

  // Build diagnostics summary — errors and warnings always, the quieter two only when present
  let counts = `${errors} errors, ${warnings} warnings`;
  if (info) counts += `, ${info} info`;
  if (hints) counts += `, ${hints} hints`;
  const lines = [`Diagnostics: ${counts}`];

  const ranked = [...findings].sort(
    (a, b) => (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9)
  );
  for (const f of ranked.slice(0, 5)) {
    const loc = f.column
      ? `${basename(f.file || filePath)}:${f.line}:${f.column}`
      : `${basename(f.file || filePath)}:${f.line}`;
    const code = f.code ? ` (${f.code})` : '';
    // pyright wraps long messages across lines — keep one finding per line
    const message = String(f.message).replace(/\s+/g, ' ').trim();
    lines.push(`   - ${loc}: ${message}${code}`);
  }
  if (ranked.length > 5) lines.push(`   ... and ${ranked.length - 5} more`);

  console.log(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PostToolUse',
      additionalContext: lines.join('\n'),
    },
  }));
}

main();
