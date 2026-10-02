import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { validateJUnit, validateCoverage } from "./ci-reports.mjs";
import { assertPortsFree, validateBrowserReports } from "./browser-ci.mjs";
import { createServer } from "node:net";

const junit =
  '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="one"/></testsuite></testsuites>';
function fixture(t) {
  const directory = mkdtempSync(resolve(tmpdir(), "seokpan-reports-"));
  t.after(() => {
    assert.equal(dirname(directory), resolve(tmpdir()));
    rmSync(directory, { recursive: true, force: true });
  });
  return directory;
}
test("JUnit accepts counted cases, not merely a nonempty XML file", (t) => {
  const path = resolve(fixture(t), "junit.xml");
  writeFileSync(path, junit);
  assert.equal(validateJUnit(path, 1), 1);
  assert.throws(() => validateJUnit(path, 2));
});
for (const xml of [
  "",
  "not xml",
  "<testsuites>",
  "<other/>",
  '<testsuite tests="0" failures="0" errors="0"/>',
  junit.replace('tests="1"', 'tests="2"'),
  junit.replace('failures="0"', 'failures="1"'),
  junit.replace('<testcase name="one"/>', '<testcase name="one"><skipped/></testcase>'),
  '<!DOCTYPE x [<!ENTITY private SYSTEM "file:///never-read">]><x>&private;</x>',
]) {
  test(`JUnit rejects invalid or failed report ${xml.slice(0, 35)}`, (t) => {
    const path = resolve(fixture(t), "junit.xml");
    writeFileSync(path, xml);
    assert.throws(() => validateJUnit(path));
  });
}
test("coverage requires matching JSON/XML and nonempty LCOV", (t) => {
  const directory = fixture(t);
  mkdirSync(resolve(directory, "coverage"));
  writeFileSync(
    resolve(directory, "coverage/coverage-summary.json"),
    JSON.stringify({ total: { lines: { total: 2, covered: 1 } } }),
  );
  const xml = resolve(directory, "coverage/cobertura-coverage.xml");
  writeFileSync(xml, '<coverage lines-valid="2" lines-covered="1"/>');
  const lcov = resolve(directory, "coverage/lcov.info");
  writeFileSync(lcov, "SF:source.ts\nDA:1,1\nend_of_record\n");
  assert.deepEqual(validateCoverage(directory), { total: 2, covered: 1 });
  writeFileSync(xml, '<coverage lines-valid="3" lines-covered="1"/>');
  assert.throws(() => validateCoverage(directory));
  writeFileSync(xml, '<coverage lines-valid="2" lines-covered="1"/>');
  writeFileSync(lcov, "");
  assert.throws(() => validateCoverage(directory));
});
test("Browser summary cannot hide failed details or missing JUnit", (t) => {
  const directory = fixture(t);
  const report = {
    stats: { expected: 1, unexpected: 0, skipped: 0, flaky: 0 },
    errors: [],
    suites: [
      {
        specs: [
          {
            ok: true,
            tests: [
              {
                expectedStatus: "passed",
                status: "expected",
                results: [{ status: "passed", retry: 0, errors: [] }],
              },
            ],
          },
        ],
      },
    ],
  };
  const json = resolve(directory, "results.json");
  writeFileSync(json, JSON.stringify(report));
  const xml = resolve(directory, "junit.xml");
  assert.throws(() => validateBrowserReports(directory));
  writeFileSync(xml, junit);
  assert.equal(validateBrowserReports(directory), 1);
  report.suites[0].specs[0].tests[0].results[0].status = "failed";
  writeFileSync(json, JSON.stringify(report));
  assert.throws(() => validateBrowserReports(directory));
});
test("occupied port is refused without stopping its owner", async (t) => {
  const server = createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => server.close());
  const { port } = server.address();
  await assert.rejects(assertPortsFree([port]), /unavailable/);
  assert.equal(server.listening, true);
});
