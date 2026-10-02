let runtimeId;

export function checkRuntime(id) {
  if (!id) return false;
  if (runtimeId && runtimeId !== id) {
    location.reload();
    return true;
  }
  runtimeId = id;
  return false;
}
