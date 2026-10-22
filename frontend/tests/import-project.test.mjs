import assert from "node:assert/strict";
import test from "node:test";
import { resolveImportTarget } from "../src/components/wizard/import-project.mjs";

test("a fresh import creates a project", () => {
  assert.deepEqual(resolveImportTarget({ requestedProjectId: null, project: null }), { action: "create" });
  assert.deepEqual(resolveImportTarget({ requestedProjectId: "  ", project: null }), { action: "create" });
});

test("returning to an existing project appends recordings to it", () => {
  assert.deepEqual(
    resolveImportTarget({
      requestedProjectId: "interview-ab12",
      project: { id: "interview-ab12", name: "Interview" },
    }),
    { action: "append", projectId: "interview-ab12", projectName: "Interview" },
  );
});

test("an unknown project is rejected instead of replaced", () => {
  assert.deepEqual(
    resolveImportTarget({ requestedProjectId: "missing-project", project: null }),
    { action: "reject", projectId: "missing-project" },
  );
  assert.deepEqual(
    resolveImportTarget({
      requestedProjectId: "missing-project",
      project: { id: "other-project", name: "Other" },
    }),
    { action: "reject", projectId: "missing-project" },
  );
});
