# Edit Pattern Taxonomy

This document defines the **exactly 22 edit patterns** used for
`fastedit-bench-01` fixtures. The taxonomy is adopted verbatim from
FastEdit's existing deterministic benchmark.

**Source citation.** `/Users/cosimo/Desktop/FastEdit/tests/test_deterministic_benchmark.py`
— docstring lines 8–29 (P1–P22 pattern names + one-line descriptions),
case collections `P1_CASES … P22_CASES` (lines 53–1645).

**Scope.** All 22 patterns are in-scope for Tier-1 (exact-match after
trailing-whitespace normalization) AND Tier-2 (AST-equivalence via
tree-sitter). There is no tier subsetting — every case regardless of
pattern is checked by both tiers (see `DESIGN.md` §5).

**Enforcement.** Each fixture YAML declares its `pattern` field as one
of the machine-readable slugs below. `fixtures/_validate.py` loads a
hard-coded allowlist of these 22 slugs and fails any case whose pattern
is not in the set (VAL-102).

---

## Pattern slug index

| Slug | FastEdit ID | Name |
|------|-------------|------|
| `add_guard` | P1 | Add guard clause |
| `add_line` | P2 | Add single line |
| `change_line` | P3 | Change one line |
| `wrap_block` | P4 | Wrap block |
| `add_at_end` | P5 | Append at end |
| `replace_block` | P6 | Replace a block |
| `change_signature` | P7 | Change signature |
| `multi_insert` | P8 | Multi-line insert |
| `class_method` | P9 | Class method edit |
| `two_markers` | P10 | Two-marker edit |
| `delete_lines` | P11 | Delete lines |
| `full_replace` | P12 | Full body replace |
| `add_branch` | P13 | Add branch |
| `extend_literal` | P14 | Extend literal |
| `add_decorator` | P15 | Add decorator |
| `rename_variable` | P16 | Rename variable |
| `reorder_statements` | P17 | Reorder statements |
| `extract_variable` | P18 | Extract variable |
| `inline_variable` | P19 | Inline variable |
| `change_expression` | P20 | Change expression |
| `add_parameter` | P21 | Add parameter |
| `remove_parameter` | P22 | Remove parameter |

---

## 1. `add_guard` — Add guard clause

Insert an early return / precondition check before the existing body of
a function.

```text
ORIGINAL:
  def average(nums):
      total = sum(nums)
      return total / len(nums)

SNIPPET:
  def average(nums):
      if not nums:
          return 0.0
      total = sum(nums)
      # ... existing code ...

EXPECTED:
  def average(nums):
      if not nums:
          return 0.0
      total = sum(nums)
      return total / len(nums)
```

**Tier scope.** Tier-1 + Tier-2.

---

## 2. `add_line` — Add single line

Insert exactly one new statement between two existing context anchors.

```text
ORIGINAL:
  def save(x):
      data = serialize(x)
      write(data)

SNIPPET:
  def save(x):
      data = serialize(x)
      logger.info("saving")
      write(data)

EXPECTED:
  def save(x):
      data = serialize(x)
      logger.info("saving")
      write(data)
```

**Tier scope.** Tier-1 + Tier-2.

---

## 3. `change_line` — Change one line

Replace exactly one line; surrounding context unchanged.

```text
ORIGINAL:
  def fetch(url):
      return requests.get(url, timeout=5)

SNIPPET:
  def fetch(url):
      return requests.get(url, timeout=30)

EXPECTED:
  def fetch(url):
      return requests.get(url, timeout=30)
```

**Tier scope.** Tier-1 + Tier-2.

---

## 4. `wrap_block` — Wrap block

Surround existing code with `try/except`, `if`, `with`, etc. via an
`... existing code ...` marker.

```text
ORIGINAL:
  def load(path):
      data = read(path)
      return parse(data)

SNIPPET:
  def load(path):
      try:
          # ... existing code ...
      except IOError:
          return None

EXPECTED:
  def load(path):
      try:
          data = read(path)
          return parse(data)
      except IOError:
          return None
```

**Tier scope.** Tier-1 + Tier-2.

---

## 5. `add_at_end` — Append at end

Insert new lines after the last pre-existing line of the symbol,
marker-guided.

```text
ORIGINAL:
  def process(x):
      y = transform(x)
      return y

SNIPPET:
  def process(x):
      # ... existing code ...
      logger.info("done")
      return y

EXPECTED:
  def process(x):
      y = transform(x)
      logger.info("done")
      return y
```

**Tier scope.** Tier-1 + Tier-2.

---

## 6. `replace_block` — Replace a block

Drop the lines between two anchors and splice new lines in their place.

```text
ORIGINAL:
  def total(items):
      acc = 0
      for i in items:
          acc += i
      return acc

SNIPPET:
  def total(items):
      return sum(items)

EXPECTED:
  def total(items):
      return sum(items)
```

**Tier scope.** Tier-1 + Tier-2.

---

## 7. `change_signature` — Change signature

Modify the function def / signature line; keep body via marker.

```text
ORIGINAL:
  def f(x):
      return x + 1

SNIPPET:
  def f(x, y=0):
      # ... existing code ...

EXPECTED:
  def f(x, y=0):
      return x + 1
```

**Tier scope.** Tier-1 + Tier-2.

---

## 8. `multi_insert` — Multi-line insert

Insert two or more new consecutive lines between two anchors.

```text
ORIGINAL:
  def setup(app):
      app.init()
      app.run()

SNIPPET:
  def setup(app):
      app.init()
      app.add_middleware(CORS)
      app.add_middleware(Auth)
      app.add_middleware(Logging)
      app.run()

EXPECTED:
  def setup(app):
      app.init()
      app.add_middleware(CORS)
      app.add_middleware(Auth)
      app.add_middleware(Logging)
      app.run()
```

**Tier scope.** Tier-1 + Tier-2.

---

## 9. `class_method` — Class method edit

Modify one method inside a class; other methods preserved via markers.

```text
ORIGINAL:
  class Store:
      def __init__(self):
          self.items = []
      def get(self, k):
          return self.items[k]
      def set(self, k, v):
          self.items[k] = v

SNIPPET:
  class Store:
      def __init__(self):
          self.items = {}
      # ... existing code ...

EXPECTED:
  class Store:
      def __init__(self):
          self.items = {}
      def get(self, k):
          return self.items[k]
      def set(self, k, v):
          self.items[k] = v
```

**Tier scope.** Tier-1 + Tier-2.

---

## 10. `two_markers` — Two-marker edit

Change a middle section; preserve top AND bottom via two separate
`... existing code ...` markers.

```text
ORIGINAL:
  def pipeline(xs):
      a = step1(xs)
      b = step2(a)
      c = step3(b)
      d = step4(c)
      return d

SNIPPET:
  def pipeline(xs):
      # ... existing code ...
      b = step2_v2(a)
      c = step3_v2(b)
      # ... existing code ...

EXPECTED:
  def pipeline(xs):
      a = step1(xs)
      b = step2_v2(a)
      c = step3_v2(b)
      d = step4(c)
      return d
```

**Tier scope.** Tier-1 + Tier-2.

---

## 11. `delete_lines` — Delete lines

Remove lines between anchors (no replacement).

```text
ORIGINAL:
  def run():
      init()
      logger.debug("starting")
      logger.debug("about to work")
      work()

SNIPPET:
  def run():
      init()
      work()

EXPECTED:
  def run():
      init()
      work()
```

**Tier scope.** Tier-1 + Tier-2.

---

## 12. `full_replace` — Full body replace

Replace the entire function body; only the signature is preserved.

```text
ORIGINAL:
  def largest(items):
      best = items[0]
      for x in items[1:]:
          if x > best:
              best = x
      return best

SNIPPET:
  def largest(items):
      return max(items)

EXPECTED:
  def largest(items):
      return max(items)
```

**Tier scope.** Tier-1 + Tier-2.

---

## 13. `add_branch` — Add branch

Add an `elif` / `else` / `case` arm to an existing conditional chain.

```text
ORIGINAL:
  def grade(score):
      if score >= 90:
          return "A"
      elif score >= 80:
          return "B"
      return "F"

SNIPPET:
  def grade(score):
      if score >= 90:
          return "A"
      elif score >= 80:
          return "B"
      elif score >= 70:
          return "C"
      return "F"

EXPECTED:
  def grade(score):
      if score >= 90:
          return "A"
      elif score >= 80:
          return "B"
      elif score >= 70:
          return "C"
      return "F"
```

**Tier scope.** Tier-1 + Tier-2.

---

## 14. `extend_literal` — Extend literal

Add items to a list / dict / set / tuple literal.

```text
ORIGINAL:
  EXTENSIONS = [".py", ".js", ".ts"]

SNIPPET:
  EXTENSIONS = [".py", ".js", ".ts", ".rs", ".go"]

EXPECTED:
  EXTENSIONS = [".py", ".js", ".ts", ".rs", ".go"]
```

**Tier scope.** Tier-1 + Tier-2.

---

## 15. `add_decorator` — Add decorator

Prepend one or more decorators above a function definition.

```text
ORIGINAL:
  def fib(n):
      if n < 2:
          return n
      return fib(n - 1) + fib(n - 2)

SNIPPET:
  @lru_cache(maxsize=128)
  def fib(n):
      # ... existing code ...

EXPECTED:
  @lru_cache(maxsize=128)
  def fib(n):
      if n < 2:
          return n
      return fib(n - 1) + fib(n - 2)
```

**Tier scope.** Tier-1 + Tier-2.

---

## 16. `rename_variable` — Rename variable

Rename a local variable throughout a function body.

```text
ORIGINAL:
  def f(xs):
      tmp = process(xs)
      tmp = tmp + 1
      return tmp

SNIPPET:
  def f(xs):
      result = process(xs)
      result = result + 1
      return result

EXPECTED:
  def f(xs):
      result = process(xs)
      result = result + 1
      return result
```

**Tier scope.** Tier-1 + Tier-2.

---

## 17. `reorder_statements` — Reorder statements

Swap the order of existing statements (no new code).

```text
ORIGINAL:
  def connect():
      connect_db()
      connect_cache()
      connect_queue()

SNIPPET:
  def connect():
      connect_cache()
      connect_db()
      connect_queue()

EXPECTED:
  def connect():
      connect_cache()
      connect_db()
      connect_queue()
```

**Tier scope.** Tier-1 + Tier-2.

---

## 18. `extract_variable` — Extract variable

Pull a sub-expression into a named local variable.

```text
ORIGINAL:
  def can_edit(user, doc):
      if user.role == "admin" or user.id == doc.owner:
          return True
      return False

SNIPPET:
  def can_edit(user, doc):
      is_admin = user.role == "admin"
      if is_admin or user.id == doc.owner:
          return True
      return False

EXPECTED:
  def can_edit(user, doc):
      is_admin = user.role == "admin"
      if is_admin or user.id == doc.owner:
          return True
      return False
```

**Tier scope.** Tier-1 + Tier-2.

---

## 19. `inline_variable` — Inline variable

Remove an assignment and inline its value at the usage site.

```text
ORIGINAL:
  def greet(first, last):
      full_name = first + " " + last
      return "Hello, " + full_name

SNIPPET:
  def greet(first, last):
      return "Hello, " + first + " " + last

EXPECTED:
  def greet(first, last):
      return "Hello, " + first + " " + last
```

**Tier scope.** Tier-1 + Tier-2.

---

## 20. `change_expression` — Change expression

Modify part of a single expression (operator, arg, string).

```text
ORIGINAL:
  def fetch(url):
      return requests.get(url)

SNIPPET:
  def fetch(url):
      return requests.get(url, timeout=30)

EXPECTED:
  def fetch(url):
      return requests.get(url, timeout=30)
```

**Tier scope.** Tier-1 + Tier-2.

---

## 21. `add_parameter` — Add parameter

Add a parameter to the signature AND update the body to use it.

```text
ORIGINAL:
  def run_task(name):
      start(name)
      finish(name)

SNIPPET:
  def run_task(name, verbose=False):
      if verbose:
          print(f"running {name}")
      start(name)
      finish(name)

EXPECTED:
  def run_task(name, verbose=False):
      if verbose:
          print(f"running {name}")
      start(name)
      finish(name)
```

**Tier scope.** Tier-1 + Tier-2.

---

## 22. `remove_parameter` — Remove parameter

Remove a parameter from the signature AND remove its usages.

```text
ORIGINAL:
  def run_task(name, debug=False):
      if debug:
          print(f"running {name}")
      start(name)
      finish(name)

SNIPPET:
  def run_task(name):
      start(name)
      finish(name)

EXPECTED:
  def run_task(name):
      start(name)
      finish(name)
```

**Tier scope.** Tier-1 + Tier-2.

---

## Adding a new pattern

If a downstream milestone (or a future fixture author) needs to introduce
a new pattern, follow these three steps in order:

1. **Pick a slug.** Lowercase, snake_case, ≤ 24 chars, verb-first
   (`split_function`, `inline_constant`). Do **not** reuse an existing
   slug.
2. **Append a section to this file.** Copy the template from any
   existing pattern above: H2 heading with slug + human name,
   one-sentence definition, minimal Python example (ORIGINAL /
   SNIPPET / EXPECTED), tier scope. Add a row to the slug index table.
3. **Extend the allowlist in `_validate.py`.** Add the slug to the
   `PATTERNS_ALLOWLIST` frozenset at the top of the file. The allowlist
   is intentionally hard-coded (not parsed from this markdown) so the
   validator has zero runtime dependencies on doc-rendering quirks.
   Run `uv run python fixtures/_validate.py --self-test` to confirm the
   validator still passes, then `uv run python fixtures/_validate.py fixtures/`
   to re-validate the existing corpus.

**Removing or renaming a pattern.** Strictly forbidden once M1 fixtures
land — doing so would silently invalidate historical JSONL results that
reference the old slug. If a pattern must be retired, mark it as
deprecated in its section here, keep its slug in the allowlist, and
refrain from authoring new cases against it.
