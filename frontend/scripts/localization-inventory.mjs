#!/usr/bin/env node

import { readdir, readFile } from "node:fs/promises"
import { createRequire } from "node:module"
import path from "node:path"
import { fileURLToPath } from "node:url"

const require = createRequire(import.meta.url)
const ts = require("typescript")

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url))
const frontendRoot = path.resolve(scriptDirectory, "..")
const projectRoot = path.resolve(frontendRoot, "..")
const sourceRoot = path.join(frontendRoot, "src")
const messagesPath = path.join(sourceRoot, "lib", "i18n", "messages.ts")

const visibleAttributeNames = new Set([
  "alt",
  "aria-description",
  "aria-label",
  "aria-valuetext",
  "caption",
  "description",
  "emptyMessage",
  "eyebrow",
  "helperText",
  "kicker",
  "label",
  "message",
  "placeholder",
  "summary",
  "text",
  "title",
  "tooltip",
])

function toProjectPath(filePath) {
  return path.relative(projectRoot, filePath).split(path.sep).join("/")
}

function compareCodePoints(left, right) {
  const leftPoints = Array.from(left, (character) => character.codePointAt(0))
  const rightPoints = Array.from(right, (character) => character.codePointAt(0))
  const length = Math.min(leftPoints.length, rightPoints.length)

  for (let index = 0; index < length; index += 1) {
    if (leftPoints[index] !== rightPoints[index]) return leftPoints[index] - rightPoints[index]
  }
  return leftPoints.length - rightPoints.length
}

function locationOf(sourceFile, node) {
  const position = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile))
  return {
    file: toProjectPath(sourceFile.fileName),
    line: position.line + 1,
    column: position.character + 1,
  }
}

function formatDiagnostic(sourceFile, diagnostic) {
  const message = ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n")
  if (diagnostic.start === undefined) {
    return `${toProjectPath(sourceFile.fileName)}: ${message}`
  }
  const position = sourceFile.getLineAndCharacterOfPosition(diagnostic.start)
  return `${toProjectPath(sourceFile.fileName)}:${position.line + 1}:${position.character + 1}: ${message}`
}

function assertParseable(sourceFile) {
  const errors = sourceFile.parseDiagnostics.filter(
    (diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error
  )
  if (errors.length > 0) {
    throw new Error(
      `TypeScript parse failed:\n${errors
        .map((diagnostic) => formatDiagnostic(sourceFile, diagnostic))
        .join("\n")}`
    )
  }
}

function unwrapExpression(expression) {
  let current = expression
  while (
    ts.isAsExpression(current) ||
    ts.isSatisfiesExpression(current) ||
    ts.isNonNullExpression(current) ||
    ts.isParenthesizedExpression(current)
  ) {
    current = current.expression
  }
  return current
}

function staticString(expression) {
  const unwrapped = unwrapExpression(expression)
  if (ts.isStringLiteral(unwrapped) || ts.isNoSubstitutionTemplateLiteral(unwrapped)) {
    return unwrapped.text
  }
  return null
}

function propertyName(property, sourceFile) {
  if (!property.name) return null
  if (
    ts.isStringLiteral(property.name) ||
    ts.isNoSubstitutionTemplateLiteral(property.name) ||
    ts.isIdentifier(property.name)
  ) {
    return property.name.text
  }
  throw new Error(
    `${toProjectPath(sourceFile.fileName)}:${locationOf(sourceFile, property).line}: ` +
      `catalog properties must use static names; found ${property.name.getText(sourceFile)}`
  )
}

function extractCatalog(sourceFile, variableName) {
  let declaration = null

  for (const statement of sourceFile.statements) {
    if (!ts.isVariableStatement(statement)) continue
    for (const candidate of statement.declarationList.declarations) {
      if (ts.isIdentifier(candidate.name) && candidate.name.text === variableName) {
        if (declaration) {
          throw new Error(`duplicate ${variableName} catalog declaration`)
        }
        declaration = candidate
      }
    }
  }

  if (!declaration?.initializer) {
    throw new Error(`messages catalog ${variableName} was not found or has no initializer`)
  }

  const initializer = unwrapExpression(declaration.initializer)
  if (!ts.isObjectLiteralExpression(initializer)) {
    throw new Error(`messages catalog ${variableName} must be an object literal`)
  }

  const entries = Object.create(null)
  for (const property of initializer.properties) {
    if (!ts.isPropertyAssignment(property)) {
      throw new Error(
        `${toProjectPath(sourceFile.fileName)}:${locationOf(sourceFile, property).line}: ` +
          `${variableName} contains a non-property entry`
      )
    }
    const key = propertyName(property, sourceFile)
    const value = staticString(property.initializer)
    if (value === null) {
      throw new Error(
        `${toProjectPath(sourceFile.fileName)}:${locationOf(sourceFile, property).line}: ` +
          `${variableName}.${key} must be a string literal`
      )
    }
    if (value.trim().length === 0) {
      throw new Error(
        `${toProjectPath(sourceFile.fileName)}:${locationOf(sourceFile, property).line}: ` +
          `${variableName}.${key} must not be empty or whitespace-only`
      )
    }
    if (Object.hasOwn(entries, key)) {
      throw new Error(`duplicate key ${JSON.stringify(key)} in ${variableName}`)
    }
    entries[key] = value
  }

  if (Object.keys(entries).length === 0) {
    throw new Error(`messages catalog ${variableName} is empty`)
  }
  return entries
}

async function sourceFiles(directory) {
  const output = []
  const entries = await readdir(directory, { withFileTypes: true })
  entries.sort((left, right) => compareCodePoints(left.name, right.name))

  for (const entry of entries) {
    const filePath = path.join(directory, entry.name)
    if (entry.isDirectory()) {
      output.push(...(await sourceFiles(filePath)))
      continue
    }
    if (entry.isFile() && /\.(?:ts|tsx)$/.test(entry.name) && !entry.name.endsWith(".d.ts")) {
      output.push(filePath)
    }
  }
  return output
}

function createSourceFile(filePath, sourceText) {
  const scriptKind = filePath.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS
  const sourceFile = ts.createSourceFile(
    filePath,
    sourceText,
    ts.ScriptTarget.Latest,
    true,
    scriptKind
  )
  assertParseable(sourceFile)
  return sourceFile
}

function normalizedVisibleText(value) {
  const normalized = value.replace(/\s+/g, " ").trim()
  return /[\p{L}\p{N}]/u.test(normalized) ? normalized : null
}

function lexicalScope(node) {
  let current = node.parent
  while (current && !ts.isSourceFile(current) && !ts.isBlock(current)) current = current.parent
  return current
}

function createStaticContext(sourceFile) {
  const declarations = new Map()
  const functions = new Map()

  function visit(node) {
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.initializer &&
      ts.isVariableDeclarationList(node.parent) &&
      (node.parent.flags & ts.NodeFlags.Const) !== 0
    ) {
      const existing = declarations.get(node.name.text) ?? []
      existing.push({ declaration: node, scope: lexicalScope(node) })
      declarations.set(node.name.text, existing)
    }
    if (ts.isFunctionDeclaration(node) && node.name && node.body) {
      const existing = functions.get(node.name.text) ?? []
      existing.push(node)
      functions.set(node.name.text, existing)
    }
    ts.forEachChild(node, visit)
  }
  visit(sourceFile)

  function declarationInitializer(identifier) {
    const candidates = (declarations.get(identifier.text) ?? [])
      .filter(({ declaration, scope }) => {
        if (declaration.getStart(sourceFile) >= identifier.getStart(sourceFile)) return false
        if (!scope || ts.isSourceFile(scope)) return true
        return (
          scope.getStart(sourceFile) <= identifier.getStart(sourceFile) &&
          scope.getEnd() >= identifier.getEnd()
        )
      })
      .sort((left, right) => {
        const leftSpan = left.scope?.getWidth(sourceFile) ?? Number.MAX_SAFE_INTEGER
        const rightSpan = right.scope?.getWidth(sourceFile) ?? Number.MAX_SAFE_INTEGER
        if (leftSpan !== rightSpan) return leftSpan - rightSpan
        return right.declaration.getStart(sourceFile) - left.declaration.getStart(sourceFile)
      })
    return candidates[0]?.declaration.initializer ?? null
  }

  return { sourceFile, declarationInitializer, functions }
}

function combinedResults(results) {
  return {
    nodes: results.flatMap((result) => result.nodes),
    complete: results.every((result) => result.complete),
  }
}

function staticPropertyName(property) {
  if (!property.name) return null
  if (
    ts.isStringLiteral(property.name) ||
    ts.isNoSubstitutionTemplateLiteral(property.name) ||
    ts.isIdentifier(property.name) ||
    ts.isNumericLiteral(property.name)
  ) {
    return property.name.text
  }
  return null
}

function propertyAssignment(objectLiteral, name) {
  for (const property of objectLiteral.properties) {
    if (
      (ts.isPropertyAssignment(property) || ts.isShorthandPropertyAssignment(property)) &&
      staticPropertyName(property) === name
    ) {
      return property
    }
  }
  return null
}

function mappedParameterValues(identifier, context, seen) {
  let current = identifier.parent
  while (current) {
    if (ts.isArrowFunction(current) || ts.isFunctionExpression(current)) {
      const parameter = current.parameters.find(
        (candidate) => ts.isIdentifier(candidate.name) && candidate.name.text === identifier.text
      )
      const call = current.parent
      if (
        parameter &&
        ts.isCallExpression(call) &&
        ts.isPropertyAccessExpression(call.expression) &&
        ["flatMap", "map"].includes(call.expression.name.text)
      ) {
        const collection = resolveValueNodes(call.expression.expression, context, seen)
        const results = []
        let complete = collection.complete
        for (const node of collection.nodes) {
          if (!ts.isArrayLiteralExpression(node)) {
            complete = false
            continue
          }
          for (const element of node.elements) {
            if (ts.isSpreadElement(element)) {
              complete = false
            } else {
              results.push(resolveValueNodes(element, context, seen))
            }
          }
        }
        const combined = combinedResults(results)
        return { nodes: combined.nodes, complete: complete && combined.complete }
      }
    }
    current = current.parent
  }
  return null
}

function resolveValueNodes(expression, context, seen = new Set()) {
  const unwrapped = unwrapExpression(expression)
  if (seen.has(unwrapped)) return { nodes: [], complete: false }
  const nextSeen = new Set(seen)
  nextSeen.add(unwrapped)

  if (
    ts.isStringLiteral(unwrapped) ||
    ts.isNoSubstitutionTemplateLiteral(unwrapped) ||
    ts.isNumericLiteral(unwrapped) ||
    unwrapped.kind === ts.SyntaxKind.NullKeyword ||
    unwrapped.kind === ts.SyntaxKind.TrueKeyword ||
    unwrapped.kind === ts.SyntaxKind.FalseKeyword ||
    ts.isObjectLiteralExpression(unwrapped) ||
    ts.isArrayLiteralExpression(unwrapped)
  ) {
    return { nodes: [unwrapped], complete: true }
  }

  if (ts.isIdentifier(unwrapped)) {
    const initializer = context.declarationInitializer(unwrapped)
    if (initializer) return resolveValueNodes(initializer, context, nextSeen)
    return mappedParameterValues(unwrapped, context, nextSeen) ?? { nodes: [], complete: false }
  }

  if (ts.isConditionalExpression(unwrapped)) {
    return combinedResults([
      resolveValueNodes(unwrapped.whenTrue, context, nextSeen),
      resolveValueNodes(unwrapped.whenFalse, context, nextSeen),
    ])
  }

  if (
    ts.isBinaryExpression(unwrapped) &&
    [
      ts.SyntaxKind.AmpersandAmpersandToken,
      ts.SyntaxKind.BarBarToken,
      ts.SyntaxKind.QuestionQuestionToken,
    ].includes(unwrapped.operatorToken.kind)
  ) {
    return combinedResults([
      resolveValueNodes(unwrapped.left, context, nextSeen),
      resolveValueNodes(unwrapped.right, context, nextSeen),
    ])
  }

  if (ts.isPropertyAccessExpression(unwrapped)) {
    const base = resolveValueNodes(unwrapped.expression, context, nextSeen)
    const results = []
    let complete = base.complete
    for (const node of base.nodes) {
      if (!ts.isObjectLiteralExpression(node)) {
        complete = false
        continue
      }
      const property = propertyAssignment(node, unwrapped.name.text)
      if (!property) {
        complete = false
      } else if (ts.isPropertyAssignment(property)) {
        results.push(resolveValueNodes(property.initializer, context, nextSeen))
      } else {
        results.push(resolveValueNodes(property.name, context, nextSeen))
      }
    }
    const combined = combinedResults(results)
    return { nodes: combined.nodes, complete: complete && combined.complete }
  }

  if (ts.isElementAccessExpression(unwrapped) && unwrapped.argumentExpression) {
    const base = resolveValueNodes(unwrapped.expression, context, nextSeen)
    const staticIndex = staticString(unwrapped.argumentExpression) ??
      (ts.isNumericLiteral(unwrapExpression(unwrapped.argumentExpression))
        ? unwrapExpression(unwrapped.argumentExpression).text
        : null)
    const results = []
    let complete = base.complete

    for (const node of base.nodes) {
      if (ts.isObjectLiteralExpression(node)) {
        const properties = staticIndex === null
          ? node.properties.filter((property) => ts.isPropertyAssignment(property))
          : [propertyAssignment(node, staticIndex)].filter(Boolean)
        if (
          staticIndex === null &&
          node.properties.some((property) => !ts.isPropertyAssignment(property))
        ) {
          complete = false
        }
        if (properties.length === 0) complete = false
        for (const property of properties) {
          if (ts.isPropertyAssignment(property)) {
            results.push(resolveValueNodes(property.initializer, context, nextSeen))
          } else {
            complete = false
          }
        }
      } else if (ts.isArrayLiteralExpression(node)) {
        const elements = staticIndex === null
          ? [...node.elements]
          : [node.elements[Number(staticIndex)]].filter(Boolean)
        if (elements.length === 0) complete = false
        for (const element of elements) {
          if (ts.isSpreadElement(element)) {
            complete = false
          } else {
            results.push(resolveValueNodes(element, context, nextSeen))
          }
        }
      } else {
        complete = false
      }
    }
    const combined = combinedResults(results)
    return { nodes: combined.nodes, complete: complete && combined.complete }
  }

  if (ts.isCallExpression(unwrapped)) {
    if (
      ts.isPropertyAccessExpression(unwrapped.expression) &&
      ["at", "filter", "find"].includes(unwrapped.expression.name.text)
    ) {
      const collection = resolveValueNodes(unwrapped.expression.expression, context, nextSeen)
      const results = []
      let complete = collection.complete
      for (const node of collection.nodes) {
        if (!ts.isArrayLiteralExpression(node)) {
          complete = false
          continue
        }
        const elements = unwrapped.expression.name.text === "at" && unwrapped.arguments[0]
          ? [node.elements[Number(staticString(unwrapped.arguments[0]) ?? unwrapped.arguments[0].getText(context.sourceFile))]].filter(Boolean)
          : [...node.elements]
        for (const element of elements) {
          if (ts.isSpreadElement(element)) {
            complete = false
          } else {
            results.push(resolveValueNodes(element, context, nextSeen))
          }
        }
      }
      const combined = combinedResults(results)
      return { nodes: combined.nodes, complete: complete && combined.complete }
    }

    if (ts.isIdentifier(unwrapped.expression)) {
      const declarations = context.functions.get(unwrapped.expression.text) ?? []
      const results = []
      for (const declaration of declarations) {
        function collectReturns(node) {
          if (node !== declaration && ts.isFunctionLike(node)) return
          if (ts.isReturnStatement(node) && node.expression) {
            results.push(resolveValueNodes(node.expression, context, nextSeen))
            return
          }
          ts.forEachChild(node, collectReturns)
        }
        collectReturns(declaration.body)
      }
      if (results.length > 0) return combinedResults(results)
    }
  }

  return { nodes: [], complete: false }
}

function staticStringCandidates(expression, context) {
  const resolved = resolveValueNodes(expression, context)
  const candidates = []
  const seen = new Set()
  let stringsOnly = resolved.nodes.length > 0

  for (const node of resolved.nodes) {
    const value = staticString(node)
    if (value === null) {
      stringsOnly = false
      continue
    }
    if (!seen.has(value)) {
      seen.add(value)
      candidates.push({ value, node })
    }
  }
  candidates.sort((left, right) => compareCodePoints(left.value, right.value))
  return { candidates, complete: resolved.complete && stringsOnly }
}

function translatorBindings(sourceFile) {
  const localeObjects = new Set()
  const translators = new Set(["t"])
  const translatorRefs = new Set()
  const declarations = []

  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.initializer) declarations.push(node)
    ts.forEachChild(node, visit)
  }
  visit(sourceFile)

  const isNamedCall = (expression, name) =>
    ts.isCallExpression(expression) &&
    ts.isIdentifier(expression.expression) &&
    expression.expression.text === name

  let changed = true
  while (changed) {
    changed = false
    const add = (set, value) => {
      if (!set.has(value)) {
        set.add(value)
        changed = true
      }
    }

    for (const declaration of declarations) {
      const initializer = unwrapExpression(declaration.initializer)
      if (ts.isIdentifier(declaration.name)) {
        const name = declaration.name.text
        if (isNamedCall(initializer, "useLocale")) add(localeObjects, name)
        if (ts.isIdentifier(initializer) && translators.has(initializer.text)) add(translators, name)
        if (
          ts.isPropertyAccessExpression(initializer) &&
          initializer.name.text === "t" &&
          ts.isIdentifier(initializer.expression) &&
          localeObjects.has(initializer.expression.text)
        ) {
          add(translators, name)
        }
        if (
          ts.isPropertyAccessExpression(initializer) &&
          initializer.name.text === "current" &&
          ts.isIdentifier(initializer.expression) &&
          translatorRefs.has(initializer.expression.text)
        ) {
          add(translators, name)
        }
        if (
          isNamedCall(initializer, "useRef") &&
          initializer.arguments[0] &&
          ts.isIdentifier(unwrapExpression(initializer.arguments[0])) &&
          translators.has(unwrapExpression(initializer.arguments[0]).text)
        ) {
          add(translatorRefs, name)
        }
      } else if (
        ts.isObjectBindingPattern(declaration.name) &&
        (isNamedCall(initializer, "useLocale") ||
          (ts.isIdentifier(initializer) && localeObjects.has(initializer.text)))
      ) {
        for (const element of declaration.name.elements) {
          const sourceName = element.propertyName?.getText(sourceFile) ?? element.name.getText(sourceFile)
          if (sourceName === "t" && ts.isIdentifier(element.name)) add(translators, element.name.text)
        }
      }
    }
  }

  return { localeObjects, translators, translatorRefs }
}

function isTranslationCallee(expression, bindings) {
  const unwrapped = unwrapExpression(expression)
  if (ts.isIdentifier(unwrapped)) return bindings.translators.has(unwrapped.text)
  if (!ts.isPropertyAccessExpression(unwrapped) || !ts.isIdentifier(unwrapped.expression)) {
    return false
  }
  return (
    (unwrapped.name.text === "current" && bindings.translatorRefs.has(unwrapped.expression.text)) ||
    (unwrapped.name.text === "t" && bindings.localeObjects.has(unwrapped.expression.text))
  )
}

function scanSource(sourceFile) {
  const calls = []
  const dynamicCalls = []
  const hardcodedCandidates = []
  const hardcodedCandidateIds = new Set()
  const staticContext = createStaticContext(sourceFile)
  const bindings = translatorBindings(sourceFile)

  function addHardcodedCandidate(kind, value, node, attribute = null) {
    const normalized = normalizedVisibleText(value)
    if (!normalized) return
    const location = locationOf(sourceFile, node)
    const id = `${kind}:${location.file}:${location.line}:${location.column}:${attribute ?? ""}:${normalized}`
    if (hardcodedCandidateIds.has(id)) return
    hardcodedCandidateIds.add(id)
    hardcodedCandidates.push({
      kind,
      ...(attribute ? { attribute } : {}),
      value: normalized,
      ...location,
    })
  }

  function visit(node) {
    if (ts.isCallExpression(node) && isTranslationCallee(node.expression, bindings)) {
      const firstArgument = node.arguments[0]
      if (!firstArgument) {
        dynamicCalls.push({
          ...locationOf(sourceFile, node),
          expression: "<missing argument>",
        })
      } else {
        const resolved = staticStringCandidates(firstArgument, staticContext)
        for (const candidate of resolved.candidates) {
          calls.push({ key: candidate.value, ...locationOf(sourceFile, candidate.node) })
        }
        if (!resolved.complete) {
          dynamicCalls.push({
            ...locationOf(sourceFile, node),
            expression: firstArgument.getText(sourceFile),
          })
        }
      }
    }

    if (ts.isJsxText(node)) {
      addHardcodedCandidate("jsx_text", node.getText(sourceFile), node)
    }

    if (ts.isJsxAttribute(node)) {
      const name = node.name.getText(sourceFile)
      if (visibleAttributeNames.has(name)) {
        if (node.initializer && ts.isStringLiteral(node.initializer)) {
          addHardcodedCandidate("jsx_attribute", node.initializer.text, node, name)
        } else if (
          node.initializer &&
          ts.isJsxExpression(node.initializer) &&
          node.initializer.expression
        ) {
          const resolved = staticStringCandidates(node.initializer.expression, staticContext)
          for (const candidate of resolved.candidates) {
            addHardcodedCandidate("jsx_attribute", candidate.value, candidate.node, name)
          }
        }
      }
    }

    if (
      ts.isJsxExpression(node) &&
      !ts.isJsxAttribute(node.parent) &&
      !node.dotDotDotToken &&
      node.expression
    ) {
      const resolved = staticStringCandidates(node.expression, staticContext)
      for (const candidate of resolved.candidates) {
        addHardcodedCandidate("jsx_expression", candidate.value, candidate.node)
      }
    }

    ts.forEachChild(node, visit)
  }

  visit(sourceFile)
  return { calls, dynamicCalls, hardcodedCandidates }
}

function placeholders(value) {
  const output = []
  const pattern = /\{([a-zA-Z][a-zA-Z0-9_]*)\}/g
  for (const match of value.matchAll(pattern)) output.push(match[1])
  return output.sort(compareCodePoints)
}

function uniqueSorted(values) {
  return [...new Set(values)].sort(compareCodePoints)
}

async function buildInventory() {
  const messagesText = await readFile(messagesPath, "utf8")
  const messagesSource = createSourceFile(messagesPath, messagesText)
  const enUS = extractCatalog(messagesSource, "enUS")
  const zhCN = extractCatalog(messagesSource, "zhCN")

  const files = await sourceFiles(sourceRoot)
  const calls = []
  const dynamicCalls = []
  const hardcodedCandidates = []

  for (const filePath of files) {
    const sourceText = filePath === messagesPath ? messagesText : await readFile(filePath, "utf8")
    const sourceFile =
      filePath === messagesPath ? messagesSource : createSourceFile(filePath, sourceText)
    const scan = scanSource(sourceFile)
    calls.push(...scan.calls)
    dynamicCalls.push(...scan.dynamicCalls)
    hardcodedCandidates.push(...scan.hardcodedCandidates)
  }

  const enKeys = Object.keys(enUS).sort(compareCodePoints)
  const zhKeys = Object.keys(zhCN).sort(compareCodePoints)
  const enKeySet = new Set(enKeys)
  const zhKeySet = new Set(zhKeys)
  const usedKeys = uniqueSorted(calls.map((call) => call.key))
  const usedKeySet = new Set(usedKeys)

  const placeholderMismatches = enKeys
    .filter((key) => zhKeySet.has(key))
    .flatMap((key) => {
      const source = placeholders(enUS[key])
      const target = placeholders(zhCN[key])
      return JSON.stringify(source) === JSON.stringify(target)
        ? []
        : [{ key, enUS: source, zhCN: target }]
    })

  const unknownOccurrences = calls.filter((call) => !enKeySet.has(call.key))
  const missingInZhCN = enKeys.filter((key) => !zhKeySet.has(key)).sort(compareCodePoints)
  const extraInZhCN = zhKeys.filter((key) => !enKeySet.has(key)).sort(compareCodePoints)
  const unknownKeys = uniqueSorted(unknownOccurrences.map((call) => call.key))
  const errors = [
    ...missingInZhCN.map((key) => `missing zhCN key: ${key}`),
    ...extraInZhCN.map((key) => `zhCN key has no enUS source: ${key}`),
    ...unknownKeys.map((key) => `unknown key used by t(): ${key}`),
    ...placeholderMismatches.map(({ key }) => `placeholder mismatch: ${key}`),
  ]

  return {
    ok: errors.length === 0,
    schemaVersion: "1.0",
    roots: {
      project: ".",
      frontend: toProjectPath(frontendRoot),
      source: toProjectPath(sourceRoot),
      messages: toProjectPath(messagesPath),
    },
    summary: {
      scannedFiles: files.length,
      enUSKeys: enKeys.length,
      zhCNKeys: zhKeys.length,
      staticTranslationCalls: calls.length,
      dynamicTranslationCalls: dynamicCalls.length,
      hardcodedCandidates: hardcodedCandidates.length,
      missingInZhCN: missingInZhCN.length,
      extraInZhCN: extraInZhCN.length,
      unknownUsedKeys: unknownKeys.length,
      unusedKeys: enKeys.filter((key) => !usedKeySet.has(key)).length,
      placeholderMismatches: placeholderMismatches.length,
    },
    catalogs: { enUS, zhCN },
    catalogValidation: {
      passed: errors.length === 0,
      errors,
      missingInZhCN,
      extraInZhCN,
      placeholderMismatches,
    },
    usage: {
      usedKeys,
      calls,
      dynamicCalls,
      unknownKeys,
      unknownOccurrences,
      unusedKeys: enKeys.filter((key) => !usedKeySet.has(key)).sort(compareCodePoints),
      unusedKeyBasis:
        "No statically resolved translator call candidate was found; unresolved dynamic references are reported separately.",
    },
    hardcodedCandidates,
  }
}

const helpText = `Usage: node frontend/scripts/localization-inventory.mjs [--help]

Build a read-only localization inventory for the MagicForge frontend.

Options:
  -h, --help  Show this help text.
`

function parseArguments(arguments_) {
  const unknown = arguments_.filter((argument) => argument !== "--help" && argument !== "-h")
  if (unknown.length > 0) {
    throw new Error(`unknown argument${unknown.length === 1 ? "" : "s"}: ${unknown.join(", ")}`)
  }
  return { help: arguments_.includes("--help") || arguments_.includes("-h") }
}

async function main(arguments_ = process.argv.slice(2)) {
  let options
  try {
    options = parseArguments(arguments_)
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    process.stderr.write(`${message}\n${helpText}`)
    return 2
  }

  if (options.help) {
    process.stdout.write(helpText)
    return 0
  }

  try {
    const inventory = await buildInventory()
    process.stdout.write(`${JSON.stringify(inventory, null, 2)}\n`)
    return inventory.ok ? 0 : 1
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    process.stdout.write(
      `${JSON.stringify({ ok: false, schemaVersion: "1.0", error: message }, null, 2)}\n`
    )
    return 1
  }
}

const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : null
if (invokedPath === fileURLToPath(import.meta.url)) {
  process.exitCode = await main()
}

export {
  buildInventory,
  compareCodePoints,
  createSourceFile,
  extractCatalog,
  main,
  parseArguments,
  scanSource,
}
