/**
 * Decide whether an import upload creates a project or appends to one.
 * An explicit project id is reused only when that project was found.
 * A missing project is rejected instead of being replaced with a new id.
 */
export function resolveImportTarget({ requestedProjectId, project }) {
  const requested = typeof requestedProjectId === "string" ? requestedProjectId.trim() : "";
  if (!requested) return { action: "create" };
  if (!project || project.id !== requested) return { action: "reject", projectId: requested };
  return { action: "append", projectId: project.id, projectName: project.name ?? project.id };
}
