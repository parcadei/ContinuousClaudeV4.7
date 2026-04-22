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
import { readFileSync } from 'fs';
import { spawnSync } from 'child_process';
import { extname, basename } from 'path';

const ENABLED_EXTENSIONS = new Set([
  '.py', '.pyx', '.pyi',                    // Python: ruff + pyright
  '.ts', '.tsx', '.js', '.jsx', '.mjs',      // JS/TS: eslint + tsc
  '.rs',                                      // Rust: cargo check + clippy
]);

function main() {
  let data;
  try { data = JSON.parse(readFileSync(0, 'utf-8')); } catch { console.log('{}'); return; }

  const editTools = new Set(['Edit', 'Write', 'MultiEdit', 'Update']);
  if (!editTools.has(data.tool_name)) { console.log('{}'); return; }

  const filePath = (data.tool_input || {}).file_path || '';
  if (!filePath) { console.log('{}'); return; }

  const ext = extname(filePath);
  if (!ENABLED_EXTENSIONS.has(ext)) { console.log('{}'); return; }

  // tldr exits non-zero when diagnostics are found — use spawnSync to capture stdout regardless.
  const proc = spawnSync('tldr', ['diagnostics', filePath, '--format', 'json'], {
    encoding: 'utf-8', timeout: 15000,
  });
  if (proc.error || !proc.stdout) { console.log('{}'); return; }

  let diag;
  try { diag = JSON.parse(proc.stdout); } catch { console.log('{}'); return; }

  // Rust tldr-cli schema: { diagnostics: [...], summary: { errors, warnings, info, hints, total } }
  const summary = diag.summary || {};
  const errorCount = summary.errors || 0;
  const warningCount = summary.warnings || 0;

  if (errorCount === 0 && warningCount === 0) { console.log('{}'); return; }

  const issues = diag.diagnostics || [];
  const lines = [`Diagnostics: ${errorCount} errors, ${warningCount} warnings`];
  for (const d of issues.slice(0, 5)) {
    const loc = d.column ? `${basename(d.file || filePath)}:${d.line}:${d.column}` : `${basename(d.file || filePath)}:${d.line}`;
    const src = d.source ? `[${d.source}] ` : '';
    lines.push(`   - ${loc}: ${src}${d.message}`);
  }
  if (issues.length > 5) lines.push(`   ... and ${issues.length - 5} more`);

  console.log(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PostToolUse',
      additionalContext: lines.join('\n'),
    },
  }));
}

main();
