import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

function xmlReport(path, inspect) {
  // Istanbul's fixed Cobertura declaration is metadata, not a resource to load.
  const xml = readFileSync(path, "utf8").replace(
    '<!DOCTYPE coverage SYSTEM "http://cobertura.sourceforge.net/xml/coverage-04.dtd">',
    "",
  );
  if (!xml.trim() || /<!DOCTYPE|<!ENTITY/i.test(xml)) throw new Error("Invalid XML report.");
  const dom = new JSDOM(xml, { contentType: "text/xml" });
  try {
    return inspect(dom.window.document);
  } finally {
    dom.window.close();
  }
}

function count(element, name) {
  const value = element.getAttribute(name);
  if (value === null || !/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)))
    throw new Error(`Invalid ${name} count.`);
  return Number(value);
}

export function validateJUnit(path, expected) {
  return xmlReport(path, (document) => {
    if (!["testsuites", "testsuite"].includes(document.documentElement.tagName))
      throw new Error("Invalid JUnit root.");
    const suites = [...document.querySelectorAll("testsuite")];
    const cases = [...document.querySelectorAll("testcase")];
    if (!suites.length || !cases.length || document.querySelector("failure,error,skipped"))
      throw new Error("JUnit has no tests or contains failures/skips.");
    let total = 0;
    for (const suite of suites) {
      if (suite.querySelector("testsuite"))
        throw new Error("Nested JUnit suites are not supported.");
      const tests = count(suite, "tests");
      if (
        count(suite, "failures") !== 0 ||
        count(suite, "errors") !== 0 ||
        (suite.hasAttribute("skipped") && count(suite, "skipped") !== 0) ||
        suite.querySelectorAll("testcase").length !== tests
      )
        throw new Error("JUnit counts disagree with test cases.");
      total += tests;
    }
    if (total !== cases.length || (expected !== undefined && total !== expected))
      throw new Error("JUnit test count mismatch.");
    const root = document.documentElement;
    if (
      (root.hasAttribute("tests") && count(root, "tests") !== total) ||
      ["errors", "failures", "skipped"].some(
        (key) => root.hasAttribute(key) && count(root, key) !== 0,
      )
    )
      throw new Error("JUnit root summary disagrees with suites.");
    return total;
  });
}

export function validateCoverage(output) {
  const summary = JSON.parse(readFileSync(`${output}/coverage/coverage-summary.json`, "utf8"));
  const lines = summary.total?.lines;
  if (
    !Number.isInteger(lines?.total) ||
    lines.total <= 0 ||
    !Number.isInteger(lines.covered) ||
    lines.covered < 0 ||
    lines.covered > lines.total
  )
    throw new Error("No valid coverage measurement.");
  xmlReport(`${output}/coverage/cobertura-coverage.xml`, (document) => {
    const root = document.documentElement;
    if (
      root.tagName !== "coverage" ||
      count(root, "lines-valid") !== lines.total ||
      count(root, "lines-covered") !== lines.covered
    )
      throw new Error("Coverage reports disagree.");
  });
  const lcov = readFileSync(`${output}/coverage/lcov.info`, "utf8");
  if (!/^SF:.+/m.test(lcov) || !/^DA:\d+,\d+/m.test(lcov) || !/^end_of_record\s*$/m.test(lcov))
    throw new Error("Invalid LCOV report.");
  return lines;
}
