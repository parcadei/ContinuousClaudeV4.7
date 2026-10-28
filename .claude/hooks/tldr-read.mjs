#!/usr/bin/env node
/**
 * PreToolUse hook — intercept Read for large code files.
 *
 * For code files >50 lines, runs `tldr extract` to build a structural
 * nav map (functions, classes, imports with line numbers), injects it
 * as additionalContext, and truncates the read for large files.
 *
 * Falls through silently if tldr is not installed.
 *
 * Bypass rules (always pass through):
 *  - Non-code files (.json, .yaml, .md, etc.)
 *  - Small files (<1500 bytes, ~50 lines)
 *  - Test files
 *  - Files under .claude/hooks/ or .claude/skills/
 *  - Targeted reads (offset/limit already set)
 */
import { readFileSync, statSync, existsSync } from 'fs';
import { spawnSync } from 'child_process';
import { extname, basename } from 'path';
import { homedir } from 'os';
import { join } from 'path';

// Resolve tldr by absolute path first: a Claude Code process started before
// ~/.cargo/bin joined PATH can't find it by name, and the failure is silent.
const CARGO_TLDR = join(homedir(), '.cargo', 'bin', process.platform === 'win32' ? 'tldr.exe' : 'tldr');
const TLDR = existsSync(CARGO_TLDR) ? CARGO_TLDR : 'tldr';

const CODE_EXTENSIONS = new Set([
  '.py', '.ts', '.tsx', '.js', '.jsx', '.mjs',
  '.go', '.rs', '.java', '.kt',
  '.c', '.cpp', '.cc', '.h', '.hpp',
  '.rb', '.php', '.swift', '.cs', '.scala',
  '.ex', '.exs', '.lua',
]);

const BYPASS_PATTERNS = [
  /\.json$/, /\.yaml$/, /\.yml$/, /\.toml$/, /\.md$/, /\.txt$/,
  /\.env/, /\.gitignore$/, /Makefile$/, /Dockerfile$/,
  // Test files — need full context for implementation
  /test_.*\.py$/, /.*_test\.py$/, /.*\.test\.[tj]sx?$/, /.*\.spec\.[tj]sx?$/,
  /.*_test\.go$/, /.*_test\.rs$/, /.*_spec\.rb$/, /.*Tests?\.kt$/,
  /.*Tests?\.swift$/, /.*Tests?\.cs$/, /.*_test\.exs?$/,
  // Own hooks and skills — we edit these
  /\.claude\/hooks\//, /\.claude\/skills\//,
];

const SIZE_THRESHOLD = 1500; // ~50 lines

function formatNavMap(info, fileName) {
  const parts = [`# ${fileName}`];

  if (info.imports && info.imports.length > 0) {
    parts.push('## Imports');
    for (const imp of info.imports.slice(0, 15)) {
      const names = imp.names ? imp.names.join(', ') : imp.module;
      parts.push(`  from ${imp.module}: ${names}`);
    }
  }

  if (info.functions && info.functions.length > 0) {
    parts.push('## Functions');
    for (const fn of info.functions.slice(0, 30)) {
      const params = fn.params ? fn.params.join(', ') : '';
      const ret = fn.return_type ? ` -> ${fn.return_type}` : '';
      const async_ = fn.is_async ? 'async ' : '';
      parts.push(`  ${async_}${fn.name}(${params})${ret}  [L${fn.line_number || fn.line || '?'}]`);
      if (fn.docstring) {
        parts.push(`    # ${fn.docstring.split('\n')[0].trim().slice(0, 100)}`);
      }
    }
  }

  if (info.classes && info.classes.length > 0) {
    parts.push('## Classes');
    for (const cls of info.classes.slice(0, 20)) {
      const bases = cls.bases && cls.bases.length ? ` (${cls.bases.join(', ')})` : '';
      parts.push(`  ${cls.name}${bases}  [L${cls.line_number || cls.line || '?'}]`);
      if (cls.fields && cls.fields.length > 0) {
        for (const f of cls.fields.slice(0, 10)) {
          const ftype = f.field_type ? `: ${f.field_type}` : '';
          parts.push(`    .${f.name}${ftype}`);
        }
        if (cls.fields.length > 10) parts.push(`    ... +${cls.fields.length - 10} fields`);
      }
      if (cls.methods && cls.methods.length > 0) {
        for (const m of cls.methods.slice(0, 8)) {
          const mparams = m.params ? m.params.join(', ') : '';
          const mret = m.return_type ? ` -> ${m.return_type}` : '';
          parts.push(`    .${m.name}(${mparams})${mret}  [L${m.line_number || m.line || '?'}]`);
        }
        if (cls.methods.length > 8) parts.push(`    ... +${cls.methods.length - 8} methods`);
      }
    }
  }

  return parts;
}

function main() {
  let data;
  try { data = JSON.parse(readFileSync(0, 'utf-8')); } catch { console.log('{}'); return; }

  if (data.tool_name !== 'Read') { console.log('{}'); return; }

  const toolInput = data.tool_input || {};
  const filePath = toolInput.file_path || '';
  const ext = extname(filePath);

  // Pass through: non-code files
  if (!CODE_EXTENSIONS.has(ext)) { console.log('{}'); return; }

  // Pass through: bypassed patterns
  if (BYPASS_PATTERNS.some(p => p.test(filePath))) { console.log('{}'); return; }

  // Pass through: targeted reads (offset/limit already set)
  if (toolInput.offset || (toolInput.limit && toolInput.limit < 100)) { console.log('{}'); return; }

  // Pass through: small files
  let fileSize;
  try { fileSize = statSync(filePath).size; } catch { console.log('{}'); return; }
  if (fileSize < SIZE_THRESHOLD) { console.log('{}'); return; }

  // Run tldr extract — falls through if tldr not installed
  let info;
  const proc = spawnSync(TLDR, ['extract', filePath, '--format', 'json'], {
    encoding: 'utf-8', timeout: 10000,
  });
  if (proc.error || !proc.stdout) { console.log('{}'); return; }
  try { info = JSON.parse(proc.stdout); } catch { console.log('{}'); return; }

  // Build nav map
  const fileName = basename(filePath);
  const parts = formatNavMap(info, fileName);

  // Only inject if we got meaningful content
  if (!parts.some(p => p.startsWith('## '))) { console.log('{}'); return; }

  parts.push('', '---', 'Read specific lines: offset=N limit=M');

  // Truncation limits based on file size
  let truncateLimit;
  if (fileSize < 3000) truncateLimit = undefined;      // ~50-100 lines: read full, just inject nav
  else if (fileSize < 10000) truncateLimit = 200;       // ~100-300 lines
  else truncateLimit = 150;                              // 300+ lines

  const updatedInput = { file_path: filePath };
  if (truncateLimit) updatedInput.limit = truncateLimit;

  let additionalContext = `[Nav Map: ${fileName}]\n\n${parts.join('\n')}`;
  if (truncateLimit) additionalContext += `\nFile truncated to ${truncateLimit} lines. Use offset/limit for more.`;

  console.log(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: 'PreToolUse',
      permissionDecision: 'allow',
      updatedInput,
      additionalContext,
    },
  }));
}

main();
