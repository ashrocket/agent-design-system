"use strict";

// Parse application TSX as data. Never import or execute the application.
const { createHash } = require("node:crypto");
const { realpathSync } = require("node:fs");
const path = require("node:path");

const MAX_INPUT_BYTES = 2 * 1024 * 1024;
const MAX_NODES = 100_000;
const MAX_CLASSES = 20_000;

async function readInput() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_INPUT_BYTES) throw new Error("TSX input exceeds adapter limit");
    chunks.push(chunk);
  }
  const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      Object.keys(value).sort().join(",") !== "path,source" ||
      typeof value.path !== "string" || typeof value.source !== "string") {
    throw new Error("invalid TSX adapter input");
  }
  return value;
}

function analyze(ts, input) {
  const file = ts.createSourceFile(input.path, input.source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const report = {
    protocol: "adsf-tailwind-tsx-v1",
    parser_version: ts.version,
    source_sha256: createHash("sha256").update(input.source, "utf8").digest("hex"),
    jsx_class_attributes: 0,
    dynamic_class_attributes: 0,
    classes: [],
    issues: [],
  };
  for (const diagnostic of file.parseDiagnostics) {
    const pos = file.getLineAndCharacterOfPosition(diagnostic.start || 0);
    report.issues.push({
      code: "tsx-syntax", line: pos.line + 1, column: pos.character + 1,
      message: ts.flattenDiagnosticMessageText(diagnostic.messageText, " "),
    });
  }
  if (report.issues.length) return report;

  let nodes = 0;
  const position = (node) => {
    const pos = file.getLineAndCharacterOfPosition(node.getStart(file));
    return { line: pos.line + 1, column: pos.character + 1 };
  };
  const addString = (value, node) => {
    for (const name of value.split(/\s+/).filter(Boolean)) {
      if (report.classes.length >= MAX_CLASSES) throw new Error("too many TSX class tokens");
      report.classes.push({ name, ...position(node) });
    }
  };
  // Returns false whenever a runtime value could introduce unobserved classes.
  const collect = (node) => {
    if (!node) return false;
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      addString(node.text, node);
      return true;
    }
    if (ts.isParenthesizedExpression(node) || ts.isAsExpression(node) ||
        ts.isTypeAssertionExpression(node) || ts.isNonNullExpression(node) ||
        ts.isSatisfiesExpression(node)) return collect(node.expression);
    if (ts.isConditionalExpression(node)) {
      const yes = collect(node.whenTrue);
      const no = collect(node.whenFalse);
      return yes && no;
    }
    if (ts.isArrayLiteralExpression(node)) {
      let complete = true;
      for (const item of node.elements) complete = collect(item) && complete;
      return complete;
    }
    if (ts.isObjectLiteralExpression(node)) {
      let complete = true;
      for (const property of node.properties) {
        if (ts.isPropertyAssignment(property) &&
            (ts.isIdentifier(property.name) || ts.isStringLiteral(property.name))) {
          addString(property.name.text, property.name);
        } else complete = false;
      }
      return complete;
    }
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) &&
        ["clsx", "classnames", "cn", "twMerge"].includes(node.expression.text)) {
      let complete = true;
      for (const arg of node.arguments) complete = collect(arg) && complete;
      return complete;
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken &&
        (ts.isStringLiteral(node.left) || ts.isNoSubstitutionTemplateLiteral(node.left)) &&
        (ts.isStringLiteral(node.right) || ts.isNoSubstitutionTemplateLiteral(node.right))) {
      addString(node.left.text + node.right.text, node);
      return true;
    }
    if (ts.isBinaryExpression(node) &&
        [ts.SyntaxKind.AmpersandAmpersandToken, ts.SyntaxKind.BarBarToken,
         ts.SyntaxKind.QuestionQuestionToken].includes(node.operatorToken.kind)) {
      collect(node.right);
      return false;
    }
    // Extract only complete static tokens from template segments. An interpolation
    // may create a class, so the containing attribute remains unverified.
    if (ts.isTemplateExpression(node)) {
      const pieces = [node.head.text, ...node.templateSpans.map((span) => span.literal.text)];
      for (let index = 0; index < pieces.length; index++) {
        const piece = pieces[index];
        const parts = piece.split(/\s+/);
        const first = index === 0 || /^\s/.test(piece) ? 0 : 1;
        const last = index === pieces.length - 1 || /\s$/.test(piece) ? parts.length : parts.length - 1;
        for (const token of parts.slice(first, last)) if (token) addString(token, node);
      }
      return false;
    }
    return false;
  };
  const visit = (node) => {
    if (++nodes > MAX_NODES) throw new Error("TSX AST exceeds adapter limit");
    if (ts.isJsxAttribute(node) && ts.isIdentifier(node.name) &&
        (node.name.text === "className" || node.name.text === "class")) {
      report.jsx_class_attributes++;
      const value = node.initializer;
      let complete = false;
      if (value && ts.isStringLiteral(value)) complete = collect(value);
      else if (value && ts.isJsxExpression(value)) complete = collect(value.expression);
      if (!complete) {
        report.dynamic_class_attributes++;
        if (report.issues.length < 1000) report.issues.push({
          code: "tsx-dynamic-class", ...position(node),
          message: "class expression may contain uninspected runtime classes",
        });
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return report;
}

async function main() {
  const resolved = realpathSync(require.resolve("typescript"));
  if (!resolved.startsWith(path.join(__dirname, "node_modules", "typescript") + path.sep)) {
    throw new Error("TypeScript must be installed beside the adapter");
  }
  const ts = require("typescript");
  if (ts.version !== "6.0.3") throw new Error("wrong TypeScript parser version");
  process.stdout.write(JSON.stringify(analyze(ts, await readInput())) + "\n");
}

main().catch((error) => {
  process.stderr.write(`Tailwind TSX adapter unavailable: ${error.message}\n`);
  process.exitCode = 2;
});
