#!/usr/bin/env bash
# readiness.sh — Deterministic agent readiness check
# Prototype for `tldr readiness` command
#
# Usage: ./readiness.sh [path]
# Output: Human summary to stderr, JSON to stdout

set -euo pipefail

TARGET="${1:-.}"
TARGET="$(cd "$TARGET" && pwd)"

# ── Colors ──────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

# ── State (indexed arrays, bash 3 compatible) ───────────────────
IDS=(); STATUSES=(); RATIONALS=(); CATS=()
PASS=0; FAIL=0; SKIP=0

record() {
  local id="$1" status="$2" rationale="$3" category="${4:-uncategorized}"
  IDS+=("$id")
  STATUSES+=("$status")
  RATIONALS+=("$rationale")
  CATS+=("$category")
  case "$status" in
    pass) PASS=$((PASS + 1)) ;;
    fail) FAIL=$((FAIL + 1)) ;;
    skip) SKIP=$((SKIP + 1)) ;;
  esac
}

# ── Helpers ─────────────────────────────────────────────────────
has_file()  { [[ -f "$TARGET/$1" ]]; }
has_dir()   { [[ -d "$TARGET/$1" ]]; }
has_glob()  { compgen -G "$TARGET/$1" > /dev/null 2>&1; }
# A "dir/" entry is checked as a directory: compgen -G returns 0 for any
# trailing-slash pattern, so globbing one is a guaranteed false positive.
has_any()   { for f in "$@"; do has_file "$f" && return 0; if [[ "$f" == */ ]]; then has_dir "${f%/}" && return 0; else has_glob "$f" && return 0; fi; done; return 1; }
file_grep() { grep -q "$1" "$TARGET/$2" 2>/dev/null; }

detect_lang() {
  has_file "package.json" && echo "typescript" && return
  has_file "pyproject.toml" && echo "python" && return
  has_file "Cargo.toml" && echo "rust" && return
  has_file "go.mod" && echo "go" && return
  has_file "build.gradle.kts" && echo "kotlin" && return
  has_file "build.gradle" && echo "java" && return
  has_file "pom.xml" && echo "java" && return
  has_file "Gemfile" && echo "ruby" && return
  has_file "composer.json" && echo "php" && return
  has_file "mix.exs" && echo "elixir" && return
  has_any "*.sln" "*.csproj" && echo "csharp" && return
  has_file "Package.swift" && echo "swift" && return
  echo "unknown"
}

find_src() {
  for d in src lib app pkg cmd; do
    [[ -d "$TARGET/$d" ]] && echo "$TARGET/$d" && return
  done
  echo "$TARGET"
}

# ── Detect environment ──────────────────────────────────────────
LANG_DETECTED=$(cd "$TARGET" && detect_lang)
SRC_DIR=$(find_src)
HAS_TLDR=$(command -v tldr >/dev/null 2>&1 && echo 1 || echo 0)

echo -e "${BOLD}Agent Readiness Check${NC}" >&2
echo -e "${DIM}Target: $TARGET${NC}" >&2
echo -e "${DIM}Language: $LANG_DETECTED${NC}" >&2
echo -e "${DIM}Source: $SRC_DIR${NC}" >&2
echo -e "${DIM}tldr: $([ "$HAS_TLDR" = 1 ] && echo available || echo missing)${NC}" >&2
echo "" >&2

# ════════════════════════════════════════════════════════════════
# CATEGORY 1: Style & Validation
# ════════════════════════════════════════════════════════════════
CAT="Style & Validation"

if has_any ".eslintrc*" "eslint.config.*" "ruff.toml" ".flake8" "clippy.toml" ".golangci.yml" \
          "phpstan.neon" "phpstan.neon.dist" ".rubocop.yml" ".credo.exs" \
          "config/detekt/detekt.yml" "checkstyle.xml" ".editorconfig"; then
  record "lint_config" "pass" "Linter config found" "$CAT"
elif file_grep "ruff" "pyproject.toml"; then
  record "lint_config" "pass" "ruff in pyproject.toml" "$CAT"
elif file_grep "detekt" "build.gradle.kts" || file_grep "checkstyle" "build.gradle"; then
  record "lint_config" "pass" "Linter in build config" "$CAT"
elif has_file "Directory.Build.props" && file_grep "EnforceCodeStyleInBuild" "Directory.Build.props"; then
  record "lint_config" "pass" ".NET analyzers enabled" "$CAT"
else
  record "lint_config" "fail" "No linter config found" "$CAT"
fi

if file_grep '"strict": true' "tsconfig.json" || file_grep '"strict":true' "tsconfig.json"; then
  record "type_check" "pass" "tsconfig strict mode enabled" "$CAT"
elif has_file "mypy.ini" || file_grep "mypy" "pyproject.toml" || file_grep "pyright" "pyproject.toml"; then
  record "type_check" "pass" "Type checker configured" "$CAT"
elif has_any "phpstan.neon" "phpstan.neon.dist"; then
  record "type_check" "pass" "PHPStan configured" "$CAT"
elif has_file "sorbet/config" || has_file "steep/Steepfile"; then
  record "type_check" "pass" "Ruby type checker configured" "$CAT"
elif [[ "$LANG_DETECTED" == "rust" || "$LANG_DETECTED" == "go" || "$LANG_DETECTED" == "java" \
     || "$LANG_DETECTED" == "kotlin" || "$LANG_DETECTED" == "csharp" || "$LANG_DETECTED" == "swift" ]]; then
  record "type_check" "pass" "Language has built-in type system" "$CAT"
elif [[ "$LANG_DETECTED" == "elixir" ]]; then
  if has_dir "priv/plts" || file_grep "dialyxir" "mix.exs"; then
    record "type_check" "pass" "Dialyzer configured" "$CAT"
  else
    record "type_check" "pass" "Dynamically typed (Dialyzer optional)" "$CAT"
  fi
else
  record "type_check" "fail" "No strict type checking" "$CAT"
fi

if has_any ".prettierrc*" "prettier.config.*" "biome.json"; then
  record "formatter" "pass" "Formatter config found" "$CAT"
elif has_any "ruff.toml" ".ruff.toml"; then
  # readiness-fix.sh writes a standalone ruff.toml; ruff format ships with it
  record "formatter" "pass" "Ruff formatter configured" "$CAT"
elif file_grep "black" "pyproject.toml" || file_grep "ruff" "pyproject.toml"; then
  record "formatter" "pass" "Python formatter configured" "$CAT"
elif has_any ".php-cs-fixer.dist.php" ".php-cs-fixer.php"; then
  record "formatter" "pass" "PHP-CS-Fixer configured" "$CAT"
elif has_file ".rubocop.yml"; then
  record "formatter" "pass" "RuboCop handles formatting" "$CAT"
elif has_file ".formatter.exs"; then
  record "formatter" "pass" "Elixir formatter configured" "$CAT"
elif has_file ".editorconfig" && file_grep "ktlint" ".editorconfig"; then
  record "formatter" "pass" "ktlint via .editorconfig" "$CAT"
elif [[ "$LANG_DETECTED" == "go" || "$LANG_DETECTED" == "rust" || "$LANG_DETECTED" == "elixir" ]]; then
  record "formatter" "pass" "Built-in formatter" "$CAT"
elif [[ "$LANG_DETECTED" == "csharp" ]] && has_file ".editorconfig"; then
  record "formatter" "pass" "dotnet format via .editorconfig" "$CAT"
elif file_grep "spotless" "build.gradle.kts" || file_grep "spotless" "build.gradle"; then
  record "formatter" "pass" "Spotless configured" "$CAT"
else
  record "formatter" "fail" "No formatter configured" "$CAT"
fi

if has_dir ".husky" || has_file ".pre-commit-config.yaml" || has_file ".lefthook.yml"; then
  record "pre_commit_hooks" "pass" "Pre-commit hooks configured" "$CAT"
else
  record "pre_commit_hooks" "fail" "No pre-commit hooks" "$CAT"
fi

# ════════════════════════════════════════════════════════════════
# CATEGORY 2: Build System
# ════════════════════════════════════════════════════════════════
CAT="Build System"

# poetry.lock was the only Python entry, so pip- and uv-managed projects could
# never pass this - including the uv-based projects readiness-fix.sh scaffolds.
if has_any "package-lock.json" "yarn.lock" "pnpm-lock.yaml" "bun.lockb" "poetry.lock" "Cargo.lock" "go.sum" \
          "composer.lock" "Gemfile.lock" "mix.lock" "gradle.lockfile" "packages.lock.json" \
          "uv.lock" "pdm.lock" "requirements.txt" "requirements.lock"; then
  record "deps_pinned" "pass" "Lockfile committed" "$CAT"
else
  record "deps_pinned" "fail" "No lockfile found" "$CAT"
fi

if has_file "Makefile" || has_file "justfile" || has_file "AGENTS.md"; then
  record "build_cmd_doc" "pass" "Build instructions present" "$CAT"
elif has_file "README.md" && file_grep -i "build\|install\|setup" "README.md"; then
  record "build_cmd_doc" "pass" "README mentions build steps" "$CAT"
else
  record "build_cmd_doc" "fail" "No build instructions found" "$CAT"
fi

if has_file "package.json" && file_grep '"dev"' "package.json"; then
  record "single_cmd_setup" "pass" "npm run dev available" "$CAT"
elif has_file "Makefile" && file_grep "dev\|setup\|start" "Makefile"; then
  record "single_cmd_setup" "pass" "make dev/setup available" "$CAT"
elif has_any "docker-compose.yml" "docker-compose.yaml" "compose.yml"; then
  record "single_cmd_setup" "pass" "docker-compose available" "$CAT"
else
  record "single_cmd_setup" "fail" "No single command setup" "$CAT"
fi

if has_file ".github/dependabot.yml" || has_any "renovate.json" ".renovaterc" ".renovaterc.json"; then
  record "dep_update_auto" "pass" "Dependabot/Renovate configured" "$CAT"
else
  record "dep_update_auto" "fail" "No dependency update automation" "$CAT"
fi

# ════════════════════════════════════════════════════════════════
# CATEGORY 3: Testing
# ════════════════════════════════════════════════════════════════
CAT="Testing"

TEST_COUNT=$(find "$TARGET" -type f \( \
  -name '*.test.*' -o -name '*.spec.*' -o -name 'test_*.py' -o -name '*_test.go' -o -name '*_test.rs' \
  -o -name '*Test.java' -o -name '*Test.kt' -o -name '*_test.exs' -o -name '*Test.php' \
  -o -name '*_spec.rb' -o -name '*Tests.cs' \
  \) -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/vendor/*' \
  -not -path '*/target/*' -not -path '*/.claude/worktrees/*' -not -path '*/.factory/worktrees/*' \
  -not -path '*/_build/*' -not -path '*/deps/*' -not -path '*/bin/*' -not -path '*/obj/*' \
  2>/dev/null | wc -l | tr -d ' ')
if [[ "$TEST_COUNT" -gt 0 ]]; then
  record "unit_tests" "pass" "$TEST_COUNT test files found" "$CAT"
else
  record "unit_tests" "fail" "No test files found" "$CAT"
fi

if has_any "playwright.config.*" "cypress.config.*" "e2e/" "tests/integration/" "tests/e2e/"; then
  record "integration_tests" "pass" "Integration/E2E test setup found" "$CAT"
else
  record "integration_tests" "fail" "No integration test setup" "$CAT"
fi

if has_any "vitest.config.*" "jest.config.*" "pytest.ini" "conftest.py" \
          "phpunit.xml" "phpunit.xml.dist" ".rspec" "spec/spec_helper.rb" \
          "test/test_helper.exs" "test/test_helper.rb"; then
  record "test_config" "pass" "Test runner configured" "$CAT"
elif file_grep "vitest\|jest" "package.json" 2>/dev/null || file_grep "pytest" "pyproject.toml" 2>/dev/null; then
  record "test_config" "pass" "Test runner in project config" "$CAT"
elif file_grep "junit\|testImplementation" "build.gradle.kts" 2>/dev/null || file_grep "junit\|testImplementation" "build.gradle" 2>/dev/null; then
  record "test_config" "pass" "JUnit in build config" "$CAT"
elif has_glob "*.Tests.csproj" || has_glob "tests/*.csproj"; then
  record "test_config" "pass" ".NET test project found" "$CAT"
else
  record "test_config" "fail" "No test runner configuration" "$CAT"
fi

# .coveragerc is what readiness-fix.sh writes for Python; the other three are JS-ecosystem
if has_dir "coverage" || has_file ".nycrc" || has_file "codecov.yml" \
   || has_file ".coveragerc" || file_grep "\[tool.coverage" "pyproject.toml"; then
  record "coverage" "pass" "Coverage reports/config present" "$CAT"
else
  record "coverage" "fail" "No coverage configuration" "$CAT"
fi

# ════════════════════════════════════════════════════════════════
# CATEGORY 4: Documentation
# ════════════════════════════════════════════════════════════════
CAT="Documentation"

if has_file "README.md" && [[ $(wc -c < "$TARGET/README.md" | tr -d ' ') -gt 100 ]]; then
  record "readme" "pass" "README.md ($(wc -l < "$TARGET/README.md" | tr -d ' ') lines)" "$CAT"
else
  record "readme" "fail" "No README.md or too short" "$CAT"
fi

if has_file "AGENTS.md" || has_file "CLAUDE.md"; then
  record "agents_md" "pass" "Agent instructions present" "$CAT"
else
  record "agents_md" "fail" "No AGENTS.md or CLAUDE.md" "$CAT"
fi

if has_dir ".claude/skills" || has_dir ".factory/skills" || has_dir ".skills"; then
  SKILL_COUNT=$(find "$TARGET" -path '*skills/*/SKILL.md' -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/vendor/*' -not -path '*/.claude/worktrees/*' -not -path '*/.factory/worktrees/*' 2>/dev/null | wc -l | tr -d ' ')
  record "skills" "pass" "$SKILL_COUNT skills found" "$CAT"
else
  record "skills" "fail" "No skills directory" "$CAT"
fi

if has_any ".env.example" ".env.template" ".env.local.example"; then
  record "env_template" "pass" "Environment template exists" "$CAT"
else
  record "env_template" "fail" "No .env.example" "$CAT"
fi

# ════════════════════════════════════════════════════════════════
# CATEGORY 5: Security
# ════════════════════════════════════════════════════════════════
CAT="Security"

if has_file ".gitignore"; then
  GI_SCORE=0
  file_grep "node_modules\|__pycache__\|target/" ".gitignore" && GI_SCORE=$((GI_SCORE + 1))
  file_grep '\.env' ".gitignore" && GI_SCORE=$((GI_SCORE + 1))
  file_grep "dist\|build\|\.next" ".gitignore" && GI_SCORE=$((GI_SCORE + 1))
  if [[ "$GI_SCORE" -ge 2 ]]; then
    record "gitignore" "pass" ".gitignore covers $GI_SCORE/3 categories" "$CAT"
  else
    record "gitignore" "fail" ".gitignore incomplete ($GI_SCORE/3)" "$CAT"
  fi
else
  record "gitignore" "fail" "No .gitignore" "$CAT"
fi

if has_any "CODEOWNERS" ".github/CODEOWNERS"; then
  record "codeowners" "pass" "CODEOWNERS file exists" "$CAT"
else
  record "codeowners" "fail" "No CODEOWNERS" "$CAT"
fi

# ════════════════════════════════════════════════════════════════
# CATEGORY 6: Task Discovery
# ════════════════════════════════════════════════════════════════
CAT="Task Discovery"

if has_dir ".github/ISSUE_TEMPLATE" || has_dir ".gitlab/issue_templates"; then
  record "issue_templates" "pass" "Issue templates configured" "$CAT"
else
  record "issue_templates" "fail" "No issue templates" "$CAT"
fi

if has_file ".github/pull_request_template.md" || has_dir ".github/PULL_REQUEST_TEMPLATE"; then
  record "pr_templates" "pass" "PR template exists" "$CAT"
else
  record "pr_templates" "fail" "No PR template" "$CAT"
fi

# ════════════════════════════════════════════════════════════════
# CATEGORY 7: Code Analysis (tldr-powered)
# ════════════════════════════════════════════════════════════════
CAT="Code Analysis (tldr)"

if [[ "$HAS_TLDR" == "1" ]]; then
  echo -e "${DIM}Running tldr analysis...${NC}" >&2

  # dead_code
  DEAD_JSON=$(cd "$TARGET" && tldr dead "$SRC_DIR" --format json --quiet 2>/dev/null || echo '{"definitely_dead":[],"possibly_dead":[],"total_functions":0}')
  DEAD_DEF=$(echo "$DEAD_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('definitely_dead',[])))" 2>/dev/null || echo 0)
  DEAD_POS=$(echo "$DEAD_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('possibly_dead',[])))" 2>/dev/null || echo 0)
  DEAD_TOT=$(echo "$DEAD_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('total_functions',0))" 2>/dev/null || echo 0)
  if [[ "$DEAD_DEF" -eq 0 ]]; then
    record "dead_code" "pass" "0 dead, $DEAD_POS possibly dead / $DEAD_TOT funcs" "$CAT"
  else
    record "dead_code" "fail" "$DEAD_DEF dead, $DEAD_POS possibly / $DEAD_TOT funcs" "$CAT"
  fi

  # clones
  CLONE_JSON=$(cd "$TARGET" && tldr clones "$SRC_DIR" --format json --quiet 2>/dev/null || echo '{"clone_pairs":[]}')
  CLONE_N=$(echo "$CLONE_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('clone_pairs',[])))" 2>/dev/null || echo 0)
  if [[ "$CLONE_N" -lt 10 ]]; then
    record "clones" "pass" "$CLONE_N clone pairs (threshold: <10)" "$CAT"
  else
    record "clones" "fail" "$CLONE_N clone pairs (threshold: <10)" "$CAT"
  fi

  # cognitive complexity
  COG_JSON=$(cd "$TARGET" && tldr cognitive "$SRC_DIR" --format json --quiet 2>/dev/null || echo '{"functions":[]}')
  COG_SEVERE=$(echo "$COG_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(d.get('summary',{}).get('severe_violations_count', len([f for f in d.get('functions',[]) if f.get('cognitive',0) >= 25])))
" 2>/dev/null || echo 0)
  if [[ "$COG_SEVERE" -lt 5 ]]; then
    record "complexity" "pass" "$COG_SEVERE severe functions (threshold: <5)" "$CAT"
  else
    record "complexity" "fail" "$COG_SEVERE severe functions (threshold: <5)" "$CAT"
  fi

  # tech debt
  DEBT_JSON=$(cd "$TARGET" && tldr debt "$SRC_DIR" --format json --quiet 2>/dev/null || echo '{"summary":{"debt_ratio":0}}')
  DEBT_R=$(echo "$DEBT_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
ratio = d.get('summary',{}).get('debt_ratio', d.get('debt_ratio',0))
print(round(ratio * 100, 1))
" 2>/dev/null || echo 0)
  if python3 -c "exit(0 if float('$DEBT_R') < 10 else 1)" 2>/dev/null; then
    record "tech_debt" "pass" "Debt ratio ${DEBT_R}% (threshold: <10%)" "$CAT"
  else
    record "tech_debt" "fail" "Debt ratio ${DEBT_R}% (threshold: <10%)" "$CAT"
  fi

  # security
  SEC_JSON=$(cd "$TARGET" && tldr secure "$SRC_DIR" --format json --quiet 2>/dev/null || echo '{}')
  SEC_N=$(echo "$SEC_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
s=d.get('summary',{})
print(s.get('taint_count',0) + s.get('leak_count',0))
" 2>/dev/null || echo 0)
  if [[ "$SEC_N" -eq 0 ]]; then
    record "security_scan" "pass" "No security issues found" "$CAT"
  else
    record "security_scan" "fail" "$SEC_N security issues" "$CAT"
  fi

  # call graph (novel)
  CG_JSON=$(cd "$TARGET" && tldr calls "$SRC_DIR" --format json --quiet 2>/dev/null || echo '{"edges":[]}')
  CG_EDGES=$(echo "$CG_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d.get('edges',[])))" 2>/dev/null || echo 0)
  CG_FILES=$(echo "$CG_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('files_analyzed',0))" 2>/dev/null || echo 0)
  if [[ "$CG_EDGES" -gt 0 ]]; then
    record "call_graph" "pass" "$CG_EDGES edges, $CG_FILES files" "$CAT"
  else
    record "call_graph" "fail" "Could not build call graph" "$CAT"
  fi

  # hotspots (novel)
  HOT_JSON=$(cd "$TARGET" && tldr hotspots "$SRC_DIR" --format json --quiet 2>/dev/null || echo '{"hotspots":[]}')
  HOT_HIGH=$(echo "$HOT_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(len([h for h in d.get('hotspots',[]) if h.get('risk_score',0) >= 0.7]))
" 2>/dev/null || echo 0)
  record "hotspots" "pass" "$HOT_HIGH high-risk hotspots" "$CAT"

else
  echo -e "${YELLOW}tldr not available - skipping code analysis${NC}" >&2
  for c in dead_code clones complexity tech_debt security_scan call_graph hotspots; do
    record "$c" "skip" "tldr not installed" "$CAT"
  done
fi

# ════════════════════════════════════════════════════════════════
# ERROR SURFACE SCORE
# ════════════════════════════════════════════════════════════════

CONSTRAINTS=0; MAX_CONSTRAINTS=11
for i in "${!IDS[@]}"; do
  id="${IDS[$i]}"
  case "$id" in
    type_check|lint_config|formatter|pre_commit_hooks|unit_tests|coverage|\
    dead_code|clones|complexity|security_scan|call_graph)
      [[ "${STATUSES[$i]}" == "pass" ]] && CONSTRAINTS=$((CONSTRAINTS + 1))
      ;;
  esac
done

ERROR_SURFACE=$(python3 -c "print(round(1.0 - ($CONSTRAINTS / $MAX_CONSTRAINTS), 2))")

# ════════════════════════════════════════════════════════════════
# OUTPUT
# ════════════════════════════════════════════════════════════════

EVALUATED=$((PASS + FAIL))
TOTAL=$((PASS + FAIL + SKIP))
PASS_RATE=$(python3 -c "print(round($PASS / max($EVALUATED,1) * 100, 1))")
LEVEL=$(python3 -c "
r = $PASS_RATE
if r >= 80: print(5)
elif r >= 60: print(4)
elif r >= 40: print(3)
elif r >= 20: print(2)
else: print(1)
")

# ── Human summary ──────────────────────────────────────────────
echo -e "" >&2
echo -e "${BOLD}════════════════════════════════════════════${NC}" >&2
echo -e "${BOLD}  Agent Readiness Report${NC}" >&2
echo -e "${BOLD}════════════════════════════════════════════${NC}" >&2
echo -e "" >&2
echo -e "  Level:         ${BOLD}$LEVEL${NC} / 5" >&2
echo -e "  Pass Rate:     ${BOLD}${PASS_RATE}%${NC}  ($PASS pass / $FAIL fail / $SKIP skip)" >&2
echo -e "  Error Surface: ${BOLD}${ERROR_SURFACE}${NC}  (0=fully constrained, 1=unconstrained)" >&2
echo -e "" >&2

PREV_CAT=""
for i in "${!IDS[@]}"; do
  cat="${CATS[$i]}"
  if [[ "$cat" != "$PREV_CAT" ]]; then
    echo -e "  ${BOLD}${cat}${NC}" >&2
    PREV_CAT="$cat"
  fi
  case "${STATUSES[$i]}" in
    pass) icon="${GREEN}PASS${NC}" ;;
    fail) icon="${RED}FAIL${NC}" ;;
    skip) icon="${YELLOW}SKIP${NC}" ;;
  esac
  printf "    %-25s [%b]  %s\n" "${IDS[$i]}" "$icon" "${RATIONALS[$i]}" >&2
done

echo -e "" >&2
echo -e "  ${BOLD}Top Actions${NC}" >&2
AC=0
for i in "${!IDS[@]}"; do
  if [[ "${STATUSES[$i]}" == "fail" && "$AC" -lt 5 ]]; then
    echo -e "    ${RED}-${NC} ${IDS[$i]}: ${RATIONALS[$i]}" >&2
    AC=$((AC + 1))
  fi
done
echo -e "" >&2

# ── JSON to stdout ─────────────────────────────────────────────
REPORT_ITEMS=""
for i in "${!IDS[@]}"; do
  case "${STATUSES[$i]}" in
    pass) NUM=1 ;;
    fail) NUM=0 ;;
    skip) NUM="null" ;;
  esac
  # Escape quotes in rationale
  RAT=$(echo "${RATIONALS[$i]}" | sed 's/"/\\"/g')
  CAT_ESC=$(echo "${CATS[$i]}" | sed 's/"/\\"/g')
  REPORT_ITEMS="${REPORT_ITEMS}\"${IDS[$i]}\": {\"numerator\": $NUM, \"denominator\": 1, \"rationale\": \"$RAT\", \"category\": \"$CAT_ESC\"},"
done
# Remove trailing comma
REPORT_ITEMS="${REPORT_ITEMS%,}"

python3 -c "
import json, datetime
report = {
    'target': '$TARGET',
    'language': '$LANG_DETECTED',
    'evaluatedAt': datetime.datetime.now().isoformat(),
    'level': $LEVEL,
    'passRate': $PASS_RATE,
    'errorSurface': $ERROR_SURFACE,
    'summary': {
        'total': $TOTAL,
        'evaluated': $EVALUATED,
        'passing': $PASS,
        'failing': $FAIL,
        'skipped': $SKIP,
        'deterministicConstraints': '$CONSTRAINTS/$MAX_CONSTRAINTS'
    },
    # json.loads, not a Python literal: SKIP criteria emit `null`, which Python can't parse
    'report': json.loads('''{$REPORT_ITEMS}''')
}
print(json.dumps(report, indent=2))
"
