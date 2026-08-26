function stripGroovyComments(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, " ")
    .replace(/(^|[^:])\/\/.*$/gm, "$1");
}

function findNamedBlock(source, blockName) {
  const matches = [
    ...source.matchAll(new RegExp(`\\b${blockName}\\s*\\{`, "g")),
  ];
  if (matches.length !== 1) {
    throw new Error(`Android Gradle must contain exactly one ${blockName} block`);
  }

  const openBrace = source.indexOf("{", matches[0].index);
  let depth = 0;
  for (let index = openBrace; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") depth -= 1;
    if (depth === 0) {
      return source.slice(openBrace + 1, index);
    }
  }
  throw new Error(`Android Gradle ${blockName} block is not closed`);
}

function normalizeExpression(expression) {
  return expression.replace(/\s+/g, " ").trim();
}

function extractDefaultConfigAssignments(source) {
  const defaultConfig = stripGroovyComments(findNamedBlock(source, "defaultConfig"));
  const assignmentPattern =
    /^\s*(versionCode|versionName)\s+([\s\S]*?)(?=^\s*(?:versionCode|versionName|testInstrumentationRunner|aaptOptions)\b)/gm;
  const assignments = new Map();

  for (const match of defaultConfig.matchAll(assignmentPattern)) {
    const name = match[1];
    if (assignments.has(name)) {
      throw new Error(`Android Gradle defaultConfig repeats ${name}`);
    }
    assignments.set(name, normalizeExpression(match[2]));
  }

  for (const name of ["versionCode", "versionName"]) {
    if (!assignments.has(name)) {
      throw new Error(`Android Gradle defaultConfig missing ${name}`);
    }
  }
  return assignments;
}

function validateAndroidGradleVersionContract(source) {
  const errors = [];
  const declarations = [
    [
      "preview signing selector",
      /def\s+previewSigningRequested\s*=\s*project\.findProperty\(["']echoPreviewSigning["']\)\?\.toString\(\)\s*==\s*["']true["']/,
    ],
    [
      "preview version-name property",
      /def\s+previewVersionName\s*=\s*project\.findProperty\(["']echoPreviewVersionName["']\)\?\.toString\(\)/,
    ],
    [
      "preview version-code property",
      /def\s+previewVersionCode\s*=\s*project\.findProperty\(["']echoPreviewVersionCode["']\)\?\.toString\(\)/,
    ],
  ];
  for (const [label, pattern] of declarations) {
    if (!pattern.test(source)) {
      errors.push(`Android Gradle missing ${label}`);
    }
  }

  let assignments;
  try {
    assignments = extractDefaultConfigAssignments(source);
  } catch (error) {
    errors.push(error.message);
    return errors;
  }

  const expected = new Map([
    [
      "versionCode",
      "previewSigningRequested ? previewVersionCode.toInteger() : currentAndroidRelease.versionCode as Integer",
    ],
    [
      "versionName",
      "previewSigningRequested ? previewVersionName : currentAndroidRelease.version.toString()",
    ],
  ]);
  for (const [name, expression] of expected) {
    if (assignments.get(name) !== expression) {
      errors.push(
        `Android Gradle ${name} must select the Preview property or append-only ledger current release`,
      );
    }
  }
  return errors;
}

module.exports = {
  extractDefaultConfigAssignments,
  validateAndroidGradleVersionContract,
};
