export async function api(path, method = "GET", body) {
  const response = await fetch("/api" + path, {
    method, headers: body && { "Content-Type": "application/json" }, body: body && JSON.stringify(body),
  });
  if (!response.ok) {
    const { detail } = await response.json().catch(() => ({}));
    throw new Error(typeof detail === "string" ? detail : `请求失败（HTTP ${response.status}）`);
  }
  return response.json();
}
