"use strict";

// Parse untrusted source as data. Never load or execute an application module.
const { createHash } = require("node:crypto");
const { realpathSync } = require("node:fs");
const path = require("node:path");

const EXPECTED_TYPESCRIPT = "6.0.3";
const MAX_INPUT_BYTES = 8 * 1024 * 1024;
const MAX_SOURCE_BYTES = 1024 * 1024;
const MAX_NODES = 20_000;
const MAX_ITEMS = 5_000;
const SAFE_TAGS = new Set([
  "article", "aside", "button", "div", "em", "footer", "h1", "h2", "h3",
  "h4", "h5", "h6", "header", "label", "li", "main", "nav", "ol",
  "p", "section", "small", "span", "strong", "ul",
]);
const SAFE_ATTRIBUTES = new Set(["aria-label", "id", "role", "title", "type"]);
const CLASS_TOKEN = /^[A-Za-z_][A-Za-z0-9_-]*$/;

async function readInput() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_INPUT_BYTES) throw new Error("input exceeds the adapter limit");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function analyze(ts, input) {
  if (
    !input || typeof input !== "object" || Array.isArray(input) ||
    Object.keys(input).sort().join(",") !== "path,source" ||
    typeof input.path !== "string" || typeof input.source !== "string" ||
    Buffer.byteLength(input.source, "utf8") > MAX_SOURCE_BYTES
  ) throw new Error("invalid or oversized adapter input");

  const source = input.source;
  const file = ts.createSourceFile(input.path, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const result = {
    protocol: "adsf-static-tsx-v1",
    parser_version: ts.version,
    source_sha256: createHash("sha256").update(source, "utf8").digest("hex"),
    imports: [],
    classes: [],
    issues: [],
  };
  let nodeCount = 0;
  let elementCount = 0;
  const fail = (node, code, message) => {
    if (result.issues.length >= 100) return;
    const offset = node ? node.getStart(file) : 0;
    const position = file.getLineAndCharacterOfPosition(offset);
    result.issues.push({ code, message, line: position.line + 1, column: position.character + 1 });
  };
  const visit = (node) => {
    nodeCount++;
    if (nodeCount > MAX_NODES) throw new Error("TSX AST exceeds node limit");
    if (ts.isJsxFragment(node)) {
      for (const child of node.children) visit(child);
      return;
    }
    if (ts.isJsxText(node)) return;
    if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) {
      elementCount++;
      if (elementCount > MAX_ITEMS) throw new Error("TSX JSX exceeds element limit");
      const opening = ts.isJsxElement(node) ? node.openingElement : node;
      const tag = opening.tagName;
      if (!ts.isIdentifier(tag) || !SAFE_TAGS.has(tag.text)) {
        fail(tag, "tsx-tag-unsupported", "only reviewed static HTML tags are allowed");
      }
      if (ts.isJsxElement(node) && node.closingElement.tagName.getText(file) !== tag.getText(file)) {
        fail(node.closingElement, "tsx-tag-mismatch", "JSX tag names do not match");
      }
      const seen = new Set();
      const initialClassCount = result.classes.length;
      for (const attr of opening.attributes.properties) {
        if (!ts.isJsxAttribute(attr) || !ts.isIdentifier(attr.name)) {
          fail(attr, "tsx-attribute-unsupported", "attribute spreads and namespaces are unsupported");
          continue;
        }
        const name = attr.name.text;
        if (seen.has(name)) fail(attr, "tsx-attribute-duplicate", "duplicate JSX attribute");
        seen.add(name);
        if (name !== "className" && !SAFE_ATTRIBUTES.has(name)) {
          fail(attr, "tsx-attribute-unsupported", `attribute ${name} is outside the static profile`);
          continue;
        }
        if (!attr.initializer || !ts.isStringLiteral(attr.initializer)) {
          fail(attr, "tsx-attribute-dynamic", "only quoted literal attributes are allowed");
          continue;
        }
        const raw = source.slice(attr.initializer.getStart(file) + 1, attr.initializer.end - 1);
        if (/[&\\]/.test(raw)) {
          fail(attr, "tsx-attribute-encoding", "entities and escapes are unsupported in JSX attributes");
          continue;
        }
        if (name === "type" && (tag.getText(file) !== "button" || !["button", "submit", "reset"].includes(raw))) {
          fail(attr, "tsx-attribute-unsupported", "type is allowed only on buttons with a reviewed value");
        }
        if (name === "className") {
          for (const token of raw.split(/[ \t\r\n\f]+/).filter(Boolean)) {
            if (!CLASS_TOKEN.test(token)) fail(attr, "tsx-class-invalid", `unsupported class token ${token}`);
            else if (result.classes.length < MAX_ITEMS) {
              const position = file.getLineAndCharacterOfPosition(attr.getStart(file));
              result.classes.push({ name: token, line: position.line + 1, column: position.character + 1 });
            } else throw new Error("TSX class references exceed limit");
          }
        }
      }
      if (result.classes.length === initialClassCount) {
        fail(opening, "tsx-class-missing", "every static JSX element needs a nonempty, literal className");
      }
      if (ts.isJsxElement(node)) for (const child of node.children) visit(child);
      return;
    }
    fail(node, "tsx-expression-unsupported", "JSX expressions and executable children are unsupported");
  };

  if (/(?:@jsx\b|@jsx(?:ImportSource|Runtime|Factory|Frag)\b|^\s*\/\/\/\s*<)/m.test(source)) {
    fail(null, "tsx-pragma-unsupported", "JSX compiler pragmas and triple-slash directives are unsupported");
  }
  for (const diagnostic of file.parseDiagnostics) {
    const offset = diagnostic.start || 0;
    const position = file.getLineAndCharacterOfPosition(offset);
    if (result.issues.length < 100) result.issues.push({
      code: "tsx-syntax",
      message: ts.flattenDiagnosticMessageText(diagnostic.messageText, " "),
      line: position.line + 1,
      column: position.character + 1,
    });
  }
  if (result.issues.some((item) => item.code === "tsx-syntax")) return result;

  let componentCount = 0;
  for (const statement of file.statements) {
    if (ts.isImportDeclaration(statement)) {
      if (
        statement.importClause || statement.assertClause || statement.attributes ||
        !ts.isStringLiteral(statement.moduleSpecifier)
      ) {
        fail(statement, "tsx-import-unsupported", "only bare local CSS imports are allowed");
        continue;
      }
      const specifier = statement.moduleSpecifier.text;
      if (
        !specifier.startsWith("./") && !specifier.startsWith("../") ||
        !specifier.endsWith(".css") || /[\\%?#]/.test(specifier)
      ) {
        fail(statement, "tsx-import-unsupported", "only plain relative .css imports are allowed");
        continue;
      }
      if (result.imports.length >= MAX_ITEMS) throw new Error("TSX imports exceed limit");
      result.imports.push(specifier);
      continue;
    }
    if (!ts.isFunctionDeclaration(statement) || componentCount++ !== 0) {
      fail(statement, "tsx-statement-unsupported", "only one exported static function is allowed");
      continue;
    }
    const modifiers = (statement.modifiers || []).map((item) => item.kind);
    if (
      modifiers.length !== 2 || modifiers[0] !== ts.SyntaxKind.ExportKeyword ||
      modifiers[1] !== ts.SyntaxKind.DefaultKeyword || !statement.name ||
      statement.asteriskToken || statement.typeParameters || statement.parameters.length ||
      statement.type || !statement.body || statement.body.statements.length !== 1 ||
      !ts.isReturnStatement(statement.body.statements[0])
    ) {
      fail(statement, "tsx-function-unsupported", "require one named, parameterless default-exported function with one return");
      continue;
    }
    let expression = statement.body.statements[0].expression;
    while (expression && ts.isParenthesizedExpression(expression)) expression = expression.expression;
    if (!expression) {
      fail(statement.body.statements[0], "tsx-return-unsupported", "function must return static JSX");
      continue;
    }
    visit(expression);
  }
  if (componentCount !== 1) fail(null, "tsx-component-count", "exactly one static component is required");
  if (!elementCount) fail(null, "tsx-empty-coverage", "no JSX elements were inspected");
  if (!result.classes.length) fail(null, "tsx-empty-classes", "no static className references were inspected");
  return result;
}

async function main() {
  const resolved = realpathSync(require.resolve("typescript"));
  const expected = path.join(__dirname, "node_modules", "typescript") + path.sep;
  if (!resolved.startsWith(expected)) throw new Error("TypeScript must be installed beside the adapter");
  const ts = require("typescript");
  if (ts.version !== EXPECTED_TYPESCRIPT) throw new Error("wrong TypeScript parser version");
  const input = await readInput();
  process.stdout.write(JSON.stringify(analyze(ts, input)) + "\n");
}

main().catch((error) => {
  process.stderr.write(`static TSX adapter unavailable: ${error.message}\n`);
  process.exitCode = 2;
});
