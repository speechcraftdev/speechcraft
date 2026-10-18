export function formatNullableQcScore(value, formatScore) {
  if (value == null) return "Not scored";
  return formatScore(value);
}
