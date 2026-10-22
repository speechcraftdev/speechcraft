/**
 * Turn a Clip Lab load failure into something the review page can show.
 * A 400 means the run has no candidate-clip manifest. That is not a dead backend,
 * and it is not a ready dataset.
 */
export function clipLabFailurePresentation({ status, detail }) {
  const message = typeof detail === "string" && detail.trim() ? detail.trim() : "";
  if (status === 400) {
    return {
      title: "Processed clips are not available",
      message: message || "This run does not have candidate clips for Clip Lab.",
      action: "speakers",
    };
  }
  if (status === 404) {
    return {
      title: "This review could not be found",
      message: message || "The dataset run is not on this Speechcraft installation.",
      action: "speakers",
    };
  }
  return {
    title: "Clip Lab could not be loaded",
    message: message || "The review service did not respond.",
    action: "retry",
  };
}
