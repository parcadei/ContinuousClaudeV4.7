#!/usr/bin/env bash
# readiness-fix.sh — Auto-fix failing readiness criteria
# Companion to readiness.sh
#
# Usage: ./readiness-fix.sh [path] [--dry-run]
# Reads readiness JSON, generates fixes for automatable failures

set -euo pipefail

TARGET="${1:-.}"
TARGET="$(cd "$TARGET" && pwd)"
DRY_RUN=0
for arg in "$@"; do [[ "$arg" == "--dry-run" ]] && DRY_RUN=1; done

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FIXED=0; SKIPPED=0

# ── Run readiness check first ─────────────────────────────────
echo -e "${BOLD}Running readiness check...${NC}"
REPORT_JSON=$("$SCRIPT_DIR/readiness.sh" "$TARGET" 2>/dev/null)
# tr -d: native Windows python writes CRLF, and a trailing \r breaks every `case` match below
LANG_DETECTED=$(echo "$REPORT_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['language'])" | tr -d '\r')

# Extract failing criteria
FAILURES=$(echo "$REPORT_JSON" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for k,v in d['report'].items():
    if v['numerator'] == 0:
        print(k)
" | tr -d '\r')

if [[ -z "$FAILURES" ]]; then
  echo -e "${GREEN}All criteria passing — nothing to fix.${NC}"
  exit 0
fi

FAIL_COUNT=$(echo "$FAILURES" | wc -l | tr -d ' ')
echo -e "${BOLD}Found $FAIL_COUNT failing criteria. Fixing what's automatable...${NC}"

# Every language-gated fixer skips when detect_lang() returns "unknown", which it
# does for any directory without a manifest file. Say so once, up front, instead of
# emitting a wall of per-criterion SKIPs that read like missing features.
if [[ "$LANG_DETECTED" == "unknown" ]]; then
  echo -e "  ${YELLOW}NOTE${NC} No language detected: readiness.sh keys off a manifest file"
  echo -e "       (pyproject.toml, package.json, Cargo.toml, go.mod, ...). Without one,"
  echo -e "       every language-specific fixer below will skip. Seed one first."
fi
echo ""

# ── Helpers ───────────────────────────────────────────────────
write_file() {
  local path="$1" content="$2" desc="$3"
  local full="$TARGET/$path"
  if [[ -f "$full" ]]; then
    echo -e "  ${YELLOW}SKIP${NC} $path (already exists)"
    SKIPPED=$((SKIPPED + 1))
    return
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    echo -e "  ${DIM}DRY${NC}  $path — $desc"
    return
  fi
  mkdir -p "$(dirname "$full")"
  echo "$content" > "$full"
  echo -e "  ${GREEN}CREATED${NC} $path — $desc"
  FIXED=$((FIXED + 1))
}

append_json_field() {
  local file="$1" field="$2" value="$3"
  if [[ ! -f "$TARGET/$file" ]]; then return; fi
  if python3 -c "import json; d=json.load(open('$TARGET/$file')); exit(0 if '$field' not in d.get('scripts',{}) else 1)" 2>/dev/null; then
    if [[ "$DRY_RUN" == "1" ]]; then
      echo -e "  ${DIM}DRY${NC}  $file — add scripts.$field"
      return
    fi
    python3 -c "
import json
with open('$TARGET/$file') as f: d=json.load(f)
d.setdefault('scripts',{})['$field'] = '$value'
with open('$TARGET/$file','w') as f: json.dump(d, f, indent=2)
print()
" 2>/dev/null
    echo -e "  ${GREEN}UPDATED${NC} $file — added scripts.$field"
    FIXED=$((FIXED + 1))
  fi
}

# ── Fix each criterion ────────────────────────────────────────
for crit in $FAILURES; do
  case "$crit" in

  lint_config)
    echo -e "${BOLD}lint_config${NC}"
    case "$LANG_DETECTED" in
      typescript|javascript)
        write_file "eslint.config.mjs" 'import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    rules: {
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      "@typescript-eslint/no-explicit-any": "warn",
    },
  },
  { ignores: ["node_modules/", "dist/", ".next/", "coverage/"] }
);' "ESLint flat config for TypeScript"
        ;;
      python)
        write_file "ruff.toml" '[lint]
select = ["E", "F", "W", "I", "N", "UP", "B", "A", "C4", "SIM"]
ignore = ["E501"]

[lint.isort]
known-first-party = []' "Ruff linter config"
        ;;
      rust)
        write_file "clippy.toml" 'cognitive-complexity-threshold = 25
too-many-arguments-threshold = 7' "Clippy config"
        ;;
      go)
        write_file ".golangci.yml" 'linters:
  enable:
    - errcheck
    - gosimple
    - govet
    - ineffassign
    - staticcheck
    - unused
    - gofmt
    - goimports' "golangci-lint config"
        ;;
      php)
        write_file "phpstan.neon" 'parameters:
    level: 8
    paths:
        - src
        - tests
    tmpDir: .cache/phpstan' "PHPStan level 8 config"
        ;;
      ruby)
        write_file ".rubocop.yml" 'require:
  - rubocop-performance

AllCops:
  TargetRubyVersion: 3.3
  NewCops: enable
  SuggestExtensions: false
  Exclude:
    - bin/**/*
    - vendor/**/*
    - tmp/**/*

Layout/LineLength:
  Max: 120

Metrics/MethodLength:
  Max: 20

Metrics/AbcSize:
  Max: 25

Metrics/BlockLength:
  Exclude:
    - spec/**/*
    - test/**/*

Style/Documentation:
  Enabled: false

Style/FrozenStringLiteralComment:
  EnforcedStyle: always' "RuboCop config"
        ;;
      java)
        write_file "checkstyle.xml" '<?xml version="1.0"?>
<!DOCTYPE module PUBLIC "-//Checkstyle//DTD Checkstyle Configuration 1.3//EN"
  "https://checkstyle.org/dtds/configuration_1_3.dtd">
<module name="Checker">
  <property name="charset" value="UTF-8"/>
  <module name="TreeWalker">
    <module name="AvoidStarImport"/>
    <module name="UnusedImports"/>
    <module name="NeedBraces"/>
    <module name="MissingSwitchDefault"/>
    <module name="FallThrough"/>
    <module name="MethodLength"><property name="max" value="60"/></module>
    <module name="CyclomaticComplexity"><property name="max" value="15"/></module>
  </module>
  <module name="FileLength"><property name="max" value="500"/></module>
  <module name="NewlineAtEndOfFile"/>
</module>' "Checkstyle config (or use google_checks.xml from JAR)"
        ;;
      kotlin)
        write_file "config/detekt/detekt.yml" 'complexity:
  LongMethod:
    threshold: 60
  TooManyFunctions:
    thresholdInFiles: 25
    thresholdInClasses: 25

style:
  MagicNumber:
    active: false
  MaxLineLength:
    maxLineLength: 120
  WildcardImport:
    active: true

formatting:
  Indentation:
    indentSize: 4
  MaximumLineLength:
    maxLineLength: 120' "Detekt config (use with buildUponDefaultConfig=true)"
        ;;
      csharp)
        write_file "Directory.Build.props" '<Project>
  <PropertyGroup>
    <AnalysisLevel>latest-recommended</AnalysisLevel>
    <EnforceCodeStyleInBuild>true</EnforceCodeStyleInBuild>
    <TreatWarningsAsErrors>false</TreatWarningsAsErrors>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
  </PropertyGroup>
</Project>' ".NET analyzers with code style enforcement"
        ;;
      elixir)
        write_file ".credo.exs" '%{
  configs: [
    %{
      name: "default",
      files: %{
        included: ["lib/", "src/", "test/"],
        excluded: [~r"/_build/", ~r"/deps/"]
      },
      strict: false,
      checks: %{
        enabled: [
          {Credo.Check.Consistency.TabsOrSpaces, []},
          {Credo.Check.Readability.MaxLineLength, [priority: :low, max_length: 120]},
          {Credo.Check.Readability.ModuleDoc, []},
          {Credo.Check.Refactor.CyclomaticComplexity, []},
          {Credo.Check.Refactor.Nesting, []},
          {Credo.Check.Warning.IoInspect, []},
          {Credo.Check.Warning.IExPry, []}
        ]
      }
    }
  ]
}' "Credo linter config"
        ;;
      *) echo -e "  ${YELLOW}SKIP${NC} Unknown language: $LANG_DETECTED"; SKIPPED=$((SKIPPED + 1)) ;;
    esac
    ;;

  type_check)
    echo -e "${BOLD}type_check${NC}"
    case "$LANG_DETECTED" in
      typescript)
        if [[ -f "$TARGET/tsconfig.json" ]]; then
          if [[ "$DRY_RUN" == "1" ]]; then
            echo -e "  ${DIM}DRY${NC}  tsconfig.json — set strict: true"
          else
            python3 -c "
import json
with open('$TARGET/tsconfig.json') as f: d=json.load(f)
d.setdefault('compilerOptions',{})['strict'] = True
with open('$TARGET/tsconfig.json','w') as f: json.dump(d, f, indent=2)
"
            echo -e "  ${GREEN}UPDATED${NC} tsconfig.json — strict: true"
            FIXED=$((FIXED + 1))
          fi
        else
          write_file "tsconfig.json" '{
  "compilerOptions": {
    "strict": true,
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "esModuleInterop": true,
    "skipLibCheck": true,
    "forceConsistentCasingInFileNames": true,
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true
  },
  "include": ["src"],
  "exclude": ["node_modules"]
}' "TypeScript config with strict mode"
        fi
        ;;
      python)
        write_file "mypy.ini" '[mypy]
python_version = 3.11
strict = True
warn_return_any = True
warn_unused_configs = True
disallow_untyped_defs = True' "mypy strict config"
        ;;
      *) echo -e "  ${YELLOW}SKIP${NC} Language has built-in types or unsupported"; SKIPPED=$((SKIPPED + 1)) ;;
    esac
    ;;

  formatter)
    echo -e "${BOLD}formatter${NC}"
    case "$LANG_DETECTED" in
      typescript|javascript)
        write_file ".prettierrc" '{
  "semi": true,
  "singleQuote": true,
  "trailingComma": "all",
  "printWidth": 100,
  "tabWidth": 2
}' "Prettier config"
        ;;
      python)
        if [[ -f "$TARGET/ruff.toml" ]]; then
          echo -e "  ${DIM}INFO${NC} ruff.toml exists — add [format] section manually if needed"
          SKIPPED=$((SKIPPED + 1))
        else
          write_file "ruff.toml" '[format]
quote-style = "double"
indent-style = "space"
line-ending = "auto"' "Ruff formatter config"
        fi
        ;;
      php)
        write_file ".php-cs-fixer.dist.php" '<?php
declare(strict_types=1);

$finder = PhpCsFixer\Finder::create()
    ->in([__DIR__ . "/src", __DIR__ . "/tests"])
    ->name("*.php");

return (new PhpCsFixer\Config())
    ->setRiskyAllowed(true)
    ->setRules([
        "@PER-CS" => true,
        "@PHP83Migration" => true,
        "strict_param" => true,
        "declare_strict_types" => true,
        "no_unused_imports" => true,
        "ordered_imports" => ["sort_algorithm" => "alpha"],
        "single_quote" => true,
        "trailing_comma_in_multiline" => true,
    ])
    ->setFinder($finder);' "PHP-CS-Fixer with PER-CS rules"
        ;;
      ruby)
        echo -e "  ${DIM}INFO${NC} RuboCop handles formatting — already covered by lint_config"
        SKIPPED=$((SKIPPED + 1))
        ;;
      kotlin)
        write_file ".editorconfig" 'root = true

[*]
charset = utf-8
end_of_line = lf
indent_size = 4
indent_style = space
insert_final_newline = true
trim_trailing_whitespace = true
max_line_length = 120

[*.{kt,kts}]
ktlint_code_style = ktlint_official' "EditorConfig with ktlint settings"
        ;;
      csharp)
        write_file ".editorconfig" 'root = true

[*]
charset = utf-8
end_of_line = lf
indent_size = 4
indent_style = space
insert_final_newline = true
trim_trailing_whitespace = true

[*.{csproj,props,targets,xml,config}]
indent_size = 2

[*.cs]
dotnet_sort_system_directives_first = true
csharp_style_var_for_built_in_types = false:suggestion
csharp_style_var_when_type_is_apparent = true:suggestion
csharp_style_pattern_matching_over_is_with_cast_check = true:warning
csharp_style_pattern_matching_over_as_with_null_check = true:warning
csharp_new_line_before_open_brace = all
csharp_new_line_before_else = true
csharp_new_line_before_catch = true
csharp_new_line_before_finally = true
dotnet_style_require_accessibility_modifiers = for_non_interface_members:warning' ".NET editorconfig for dotnet format"
        ;;
      elixir)
        write_file ".formatter.exs" '[
  inputs: [
    "{mix,.formatter,.credo}.exs",
    "{config,lib,test}/**/*.{ex,exs}"
  ],
  line_length: 120
]' "Elixir formatter config"
        ;;
      *) echo -e "  ${YELLOW}SKIP${NC} Language has built-in formatter or unsupported"; SKIPPED=$((SKIPPED + 1)) ;;
    esac
    ;;

  pre_commit_hooks)
    echo -e "${BOLD}pre_commit_hooks${NC}"
    case "$LANG_DETECTED" in
      typescript|javascript)
        if [[ "$DRY_RUN" == "1" ]]; then
          echo -e "  ${DIM}DRY${NC}  .husky/pre-commit"
        else
          mkdir -p "$TARGET/.husky"
          cat > "$TARGET/.husky/pre-commit" << 'HOOK'
#!/bin/sh
npx lint-staged
HOOK
          chmod +x "$TARGET/.husky/pre-commit"
          echo -e "  ${GREEN}CREATED${NC} .husky/pre-commit"
          # lint-staged config
          if [[ ! -f "$TARGET/.lintstagedrc" && ! -f "$TARGET/.lintstagedrc.json" ]]; then
            echo '{ "*.{ts,tsx,js,jsx}": ["eslint --fix", "prettier --write"], "*.{json,md}": ["prettier --write"] }' > "$TARGET/.lintstagedrc.json"
            echo -e "  ${GREEN}CREATED${NC} .lintstagedrc.json"
          fi
          FIXED=$((FIXED + 1))
        fi
        ;;
      python)
        # Hook revs go stale silently: a project scaffolded today would lint under
        # ruff 0.8.0 (Nov 2024) while its developers run a current ruff, and the two
        # disagree. Run `pre-commit autoupdate` after generating to track upstream.
        write_file ".pre-commit-config.yaml" 'repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.16.8
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v2.3.1
    hooks:
      - id: mypy' "pre-commit config with ruff + mypy"
        ;;
      *) echo -e "  ${YELLOW}SKIP${NC} Add pre-commit hooks manually for $LANG_DETECTED"; SKIPPED=$((SKIPPED + 1)) ;;
    esac
    ;;

  deps_pinned)
    echo -e "${BOLD}deps_pinned${NC}"
    echo -e "  ${YELLOW}SKIP${NC} Run your package manager install to generate lockfile"
    SKIPPED=$((SKIPPED + 1))
    ;;

  build_cmd_doc)
    echo -e "${BOLD}build_cmd_doc${NC}"
    case "$LANG_DETECTED" in
      typescript|javascript)
        write_file "Makefile" '.PHONY: dev build test lint clean

dev:
	npm run dev

build:
	npm run build

test:
	npm test

lint:
	npm run lint

clean:
	rm -rf node_modules .next dist coverage' "Makefile with standard npm targets"
        ;;
      python)
        write_file "Makefile" '.PHONY: dev test lint fmt clean

dev:
	uv run python -m $(shell basename $(CURDIR))

test:
	uv run pytest

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	rm -rf .pytest_cache .ruff_cache .mypy_cache' "Makefile with Python targets"
        ;;
      rust)
        write_file "Makefile" '.PHONY: dev build test lint clean

dev:
	cargo run

build:
	cargo build --release

test:
	cargo test

lint:
	cargo clippy -- -D warnings

clean:
	cargo clean' "Makefile with Cargo targets"
        ;;
      go)
        write_file "Makefile" '.PHONY: dev build test lint clean

dev:
	go run .

build:
	go build -o bin/app .

test:
	go test ./...

lint:
	golangci-lint run

clean:
	rm -rf bin/' "Makefile with Go targets"
        ;;
      php)
        write_file "Makefile" '.PHONY: test lint fmt analyse clean

test:
	vendor/bin/phpunit

lint:
	vendor/bin/phpstan analyse

fmt:
	vendor/bin/php-cs-fixer fix

analyse:
	vendor/bin/phpstan analyse --memory-limit=512M

clean:
	rm -rf .cache/ .phpunit.cache/ vendor/' "Makefile with PHP targets"
        ;;
      ruby)
        write_file "Makefile" '.PHONY: test lint fmt console clean

test:
	bundle exec rspec

lint:
	bundle exec rubocop

fmt:
	bundle exec rubocop -a

console:
	bundle exec irb

clean:
	rm -rf coverage/ tmp/' "Makefile with Ruby targets"
        ;;
      java)
        if [[ -f "$TARGET/pom.xml" ]]; then
          write_file "Makefile" 'MVN := $(shell [ -f ./mvnw ] && echo ./mvnw || echo mvn)

.PHONY: build test lint fmt clean

build:
	$(MVN) compile

test:
	$(MVN) test

lint:
	$(MVN) checkstyle:check

fmt:
	$(MVN) spotless:apply

clean:
	$(MVN) clean' "Makefile with Maven targets"
        else
          write_file "Makefile" '.PHONY: build test lint clean

build:
	./gradlew build

test:
	./gradlew test

lint:
	./gradlew checkstyleMain

clean:
	./gradlew clean' "Makefile with Gradle targets"
        fi
        ;;
      kotlin)
        write_file "Makefile" '.PHONY: build test lint clean

build:
	./gradlew build

test:
	./gradlew test

lint:
	./gradlew detekt

clean:
	./gradlew clean' "Makefile with Kotlin/Gradle targets"
        ;;
      csharp)
        write_file "Makefile" '.PHONY: build test lint fmt clean

build:
	dotnet build

test:
	dotnet test

lint:
	dotnet build /p:EnforceCodeStyleInBuild=true /warnaserror

fmt:
	dotnet format

clean:
	dotnet clean' "Makefile with .NET targets"
        ;;
      elixir)
        write_file "Makefile" '.PHONY: deps test lint fmt clean

deps:
	mix deps.get

test:
	mix test

lint:
	mix credo --strict

fmt:
	mix format

clean:
	rm -rf _build/ deps/' "Makefile with Mix targets"
        ;;
      *) echo -e "  ${YELLOW}SKIP${NC} Unknown language"; SKIPPED=$((SKIPPED + 1)) ;;
    esac
    ;;

  single_cmd_setup)
    echo -e "${BOLD}single_cmd_setup${NC}"
    if [[ -f "$TARGET/package.json" ]]; then
      append_json_field "package.json" "dev" "next dev"
    else
      echo -e "  ${YELLOW}SKIP${NC} No package.json — add dev command manually"
      SKIPPED=$((SKIPPED + 1))
    fi
    ;;

  dep_update_auto)
    echo -e "${BOLD}dep_update_auto${NC}"
    local_eco="npm"
    case "$LANG_DETECTED" in
      python) local_eco="pip" ;;
      rust) local_eco="cargo" ;;
      go) local_eco="gomod" ;;
      php) local_eco="composer" ;;
      ruby) local_eco="bundler" ;;
      java)
        if [[ -f "$TARGET/build.gradle" || -f "$TARGET/build.gradle.kts" ]]; then
          local_eco="gradle"
        else
          local_eco="maven"
        fi ;;
      kotlin) local_eco="gradle" ;;
      csharp) local_eco="nuget" ;;
      elixir) local_eco="mix" ;;
    esac
    write_file ".github/dependabot.yml" "version: 2
updates:
  - package-ecosystem: \"$local_eco\"
    directory: \"/\"
    schedule:
      interval: \"weekly\"
    open-pull-requests-limit: 10" "Dependabot config for $local_eco"
    ;;

  unit_tests)
    echo -e "${BOLD}unit_tests${NC}"
    echo -e "  ${YELLOW}SKIP${NC} Tests require understanding of the codebase — write them manually"
    SKIPPED=$((SKIPPED + 1))
    ;;

  integration_tests)
    echo -e "${BOLD}integration_tests${NC}"
    echo -e "  ${YELLOW}SKIP${NC} Integration tests require understanding of the system"
    SKIPPED=$((SKIPPED + 1))
    ;;

  test_config)
    echo -e "${BOLD}test_config${NC}"
    case "$LANG_DETECTED" in
      typescript|javascript)
        write_file "vitest.config.ts" 'import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    globals: true,
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}", "tests/**/*.test.{ts,tsx}"],
    coverage: {
      provider: "v8",
      reporter: ["text", "json", "html"],
      exclude: ["node_modules/", "dist/", ".next/"],
    },
  },
});' "Vitest config with coverage"
        ;;
      python)
        write_file "conftest.py" '"""Root conftest for pytest."""
import pytest' "pytest conftest"
        ;;
      php)
        write_file "phpunit.xml.dist" '<?xml version="1.0" encoding="UTF-8"?>
<phpunit xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:noNamespaceSchemaLocation="vendor/phpunit/phpunit/phpunit.xsd"
         bootstrap="vendor/autoload.php"
         colors="true"
         failOnRisky="true"
         failOnWarning="true"
         cacheDirectory=".phpunit.cache">
    <testsuites>
        <testsuite name="Unit">
            <directory>tests/Unit</directory>
        </testsuite>
        <testsuite name="Integration">
            <directory>tests/Integration</directory>
        </testsuite>
    </testsuites>
    <source>
        <include>
            <directory>src</directory>
        </include>
    </source>
</phpunit>' "PHPUnit 11+ config"
        ;;
      ruby)
        write_file ".rspec" '--format documentation
--color
--require spec_helper' "RSpec config"
        ;;
      elixir)
        write_file "test/test_helper.exs" 'ExUnit.start()' "ExUnit test helper"
        ;;
      *) echo -e "  ${YELLOW}SKIP${NC} Test config managed by build tool"; SKIPPED=$((SKIPPED + 1)) ;;
    esac
    ;;

  coverage)
    echo -e "${BOLD}coverage${NC}"
    case "$LANG_DETECTED" in
      typescript|javascript)
        write_file "codecov.yml" 'coverage:
  status:
    project:
      default:
        target: 70%
        threshold: 5%
    patch:
      default:
        target: 80%' "Codecov config"
        ;;
      python)
        write_file ".coveragerc" '[run]
source = src
omit = tests/*

[report]
show_missing = true
fail_under = 70' "Coverage config"
        ;;
      php)
        echo -e "  ${DIM}INFO${NC} PHPUnit coverage requires xdebug/pcov — add to phpunit.xml.dist manually"
        SKIPPED=$((SKIPPED + 1))
        ;;
      ruby)
        echo -e "  ${DIM}INFO${NC} Add simplecov to Gemfile: gem 'simplecov', require: false, group: :test"
        SKIPPED=$((SKIPPED + 1))
        ;;
      elixir)
        echo -e "  ${DIM}INFO${NC} Use mix test --cover (built-in) or add excoveralls to mix.exs"
        SKIPPED=$((SKIPPED + 1))
        ;;
      *) echo -e "  ${YELLOW}SKIP${NC} Coverage managed by build tool or not applicable"; SKIPPED=$((SKIPPED + 1)) ;;
    esac
    ;;

  readme)
    echo -e "${BOLD}readme${NC}"
    PROJECT_NAME=$(basename "$TARGET")
    write_file "README.md" "# $PROJECT_NAME

## Setup

\`\`\`bash
# Install dependencies
# TODO: Add install command

# Run development server
# TODO: Add dev command
\`\`\`

## Testing

\`\`\`bash
# TODO: Add test command
\`\`\`

## Architecture

TODO: Describe the architecture." "README scaffold"
    ;;

  agents_md)
    echo -e "${BOLD}agents_md${NC}"
    PROJECT_NAME=$(basename "$TARGET")
    BUILD_CMD="# TODO"; TEST_CMD="# TODO"; LINT_CMD="# TODO"
    case "$LANG_DETECTED" in
      typescript|javascript)
        if [[ -f "$TARGET/package.json" ]]; then
          BUILD_CMD=$(python3 -c "import json; s=json.load(open('$TARGET/package.json')).get('scripts',{}); print(f'npm run {next(k for k in [\"build\",\"compile\"] if k in s)}' if any(k in s for k in ['build','compile']) else '# TODO')" 2>/dev/null || echo "# TODO")
          TEST_CMD=$(python3 -c "import json; s=json.load(open('$TARGET/package.json')).get('scripts',{}); print('npm test' if 'test' in s else '# TODO')" 2>/dev/null || echo "# TODO")
          LINT_CMD=$(python3 -c "import json; s=json.load(open('$TARGET/package.json')).get('scripts',{}); print('npm run lint' if 'lint' in s else '# TODO')" 2>/dev/null || echo "# TODO")
        fi ;;
      python) BUILD_CMD="uv run python -m app"; TEST_CMD="uv run pytest"; LINT_CMD="uv run ruff check ." ;;
      rust) BUILD_CMD="cargo build --release"; TEST_CMD="cargo test"; LINT_CMD="cargo clippy -- -D warnings" ;;
      go) BUILD_CMD="go build ./..."; TEST_CMD="go test ./..."; LINT_CMD="golangci-lint run" ;;
      php) BUILD_CMD="composer install"; TEST_CMD="vendor/bin/phpunit"; LINT_CMD="vendor/bin/phpstan analyse" ;;
      ruby) BUILD_CMD="bundle install"; TEST_CMD="bundle exec rspec"; LINT_CMD="bundle exec rubocop" ;;
      java)
        if [[ -f "$TARGET/pom.xml" ]]; then
          BUILD_CMD="mvn compile"; TEST_CMD="mvn test"; LINT_CMD="mvn checkstyle:check"
        else
          BUILD_CMD="./gradlew build"; TEST_CMD="./gradlew test"; LINT_CMD="./gradlew checkstyleMain"
        fi ;;
      kotlin) BUILD_CMD="./gradlew build"; TEST_CMD="./gradlew test"; LINT_CMD="./gradlew detekt" ;;
      csharp) BUILD_CMD="dotnet build"; TEST_CMD="dotnet test"; LINT_CMD="dotnet format --verify-no-changes" ;;
      elixir) BUILD_CMD="mix compile"; TEST_CMD="mix test"; LINT_CMD="mix credo --strict" ;;
    esac
    write_file "CLAUDE.md" "# $PROJECT_NAME

## Build & Test Commands

\`\`\`bash
# Build
$BUILD_CMD

# Test
$TEST_CMD

# Lint
$LINT_CMD
\`\`\`

## Architecture

Language: $LANG_DETECTED

## Conventions

- Follow existing code patterns
- Write tests before implementation
- Run lint before committing" "CLAUDE.md with detected commands"
    ;;

  env_template)
    echo -e "${BOLD}env_template${NC}"
    if [[ -f "$TARGET/.env" ]]; then
      if [[ "$DRY_RUN" == "1" ]]; then
        echo -e "  ${DIM}DRY${NC}  .env.example — strip values from .env"
      else
        sed 's/=.*/=/' "$TARGET/.env" > "$TARGET/.env.example"
        echo -e "  ${GREEN}CREATED${NC} .env.example — stripped values from .env"
        FIXED=$((FIXED + 1))
      fi
    elif [[ -f "$TARGET/.env.local" ]]; then
      if [[ "$DRY_RUN" == "1" ]]; then
        echo -e "  ${DIM}DRY${NC}  .env.example — strip values from .env.local"
      else
        sed 's/=.*/=/' "$TARGET/.env.local" > "$TARGET/.env.example"
        echo -e "  ${GREEN}CREATED${NC} .env.example — stripped values from .env.local"
        FIXED=$((FIXED + 1))
      fi
    else
      write_file ".env.example" "# Add environment variables here
# DATABASE_URL=
# API_KEY=" "Empty .env.example template"
    fi
    ;;

  gitignore)
    echo -e "${BOLD}gitignore${NC}"
    case "$LANG_DETECTED" in
      typescript|javascript)
        write_file ".gitignore" 'node_modules/
dist/
.next/
build/
coverage/
.env
.env.local
.env*.local
*.tsbuildinfo
.DS_Store' "Node.js .gitignore"
        ;;
      python)
        write_file ".gitignore" '__pycache__/
*.py[cod]
*.egg-info/
dist/
build/
.venv/
.env
.mypy_cache/
.ruff_cache/
.pytest_cache/
htmlcov/
.coverage
.DS_Store' "Python .gitignore"
        ;;
      rust)
        write_file ".gitignore" 'target/
.env
.DS_Store' "Rust .gitignore"
        ;;
      go)
        write_file ".gitignore" 'bin/
.env
vendor/
.DS_Store' "Go .gitignore"
        ;;
      php)
        write_file ".gitignore" '/vendor/
/node_modules/
/.cache/
.php-cs-fixer.cache
.phpunit.result.cache
.phpunit.cache/
phpunit.xml
.env
.env.local
.DS_Store' "PHP .gitignore"
        ;;
      ruby)
        write_file ".gitignore" '/vendor/bundle/
/.bundle/
/coverage/
/tmp/
/log/
*.gem
.env
.byebug_history
.DS_Store' "Ruby .gitignore"
        ;;
      java)
        write_file ".gitignore" '.gradle/
build/
!gradle/wrapper/gradle-wrapper.jar
*.class
*.jar
*.war
.idea/
*.iml
out/
.env
.DS_Store' "Java/Gradle .gitignore"
        ;;
      kotlin)
        write_file ".gitignore" '.gradle/
build/
!gradle/wrapper/gradle-wrapper.jar
.kotlin/
.idea/
*.iml
out/
local.properties
.env
.DS_Store' "Kotlin/Gradle .gitignore"
        ;;
      csharp)
        write_file ".gitignore" '[Dd]ebug/
[Rr]elease/
[Bb]in/
[Oo]bj/
.vs/
*.suo
*.user
*.nupkg
[Tt]est[Rr]esult*/
coverage/
artifacts/
.env
.DS_Store' ".NET .gitignore"
        ;;
      elixir)
        write_file ".gitignore" '/_build/
/deps/
/cover/
/doc/
/rel/
/priv/plts/
.elixir_ls/
.env
*.secret.exs
.DS_Store' "Elixir .gitignore"
        ;;
      *)
        write_file ".gitignore" '.env
.DS_Store
*.log' "Minimal .gitignore"
        ;;
    esac
    ;;

  codeowners)
    echo -e "${BOLD}codeowners${NC}"
    echo -e "  ${YELLOW}SKIP${NC} CODEOWNERS requires team knowledge — create manually"
    SKIPPED=$((SKIPPED + 1))
    ;;

  issue_templates)
    echo -e "${BOLD}issue_templates${NC}"
    write_file ".github/ISSUE_TEMPLATE/bug_report.md" '---
name: Bug Report
about: Report a bug
labels: bug
---

## Description

## Steps to Reproduce
1.
2.
3.

## Expected Behavior

## Actual Behavior

## Environment
- OS:
- Version:' "Bug report template"
    write_file ".github/ISSUE_TEMPLATE/feature_request.md" '---
name: Feature Request
about: Suggest a feature
labels: enhancement
---

## Problem

## Proposed Solution

## Alternatives Considered' "Feature request template"
    ;;

  pr_templates)
    echo -e "${BOLD}pr_templates${NC}"
    write_file ".github/pull_request_template.md" '## Summary

## Changes

## Testing

- [ ] Tests added/updated
- [ ] Tested locally
- [ ] No breaking changes' "PR template"
    ;;

  # tldr-powered criteria — not auto-fixable
  dead_code|clones|complexity|tech_debt|security_scan)
    echo -e "${BOLD}$crit${NC}"
    echo -e "  ${YELLOW}SKIP${NC} Requires code changes — use \`tldr $crit\` to identify issues"
    SKIPPED=$((SKIPPED + 1))
    ;;

  *)
    echo -e "${BOLD}$crit${NC}"
    echo -e "  ${YELLOW}SKIP${NC} No auto-fix available"
    SKIPPED=$((SKIPPED + 1))
    ;;

  esac
  echo ""
done

# ── Summary ───────────────────────────────────────────────────
echo -e "${BOLD}════════════════════════════════════════════${NC}"
if [[ "$DRY_RUN" == "1" ]]; then
  echo -e "${BOLD}  Dry Run Complete${NC}"
  echo -e "  Would fix: ${BOLD}$FAIL_COUNT${NC} criteria"
else
  echo -e "${BOLD}  Fix Complete${NC}"
  echo -e "  Fixed:   ${GREEN}$FIXED${NC}"
  echo -e "  Skipped: ${YELLOW}$SKIPPED${NC}"
fi
echo -e "${BOLD}════════════════════════════════════════════${NC}"

if [[ "$DRY_RUN" != "1" && "$FIXED" -gt 0 ]]; then
  echo ""
  echo -e "${DIM}Re-run readiness check:${NC}"
  echo -e "  $SCRIPT_DIR/readiness.sh $TARGET"
fi
