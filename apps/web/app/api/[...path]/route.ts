import { NextRequest } from "next/server";

async function proxy(request: NextRequest, context: { params: Promise<{path: string[]}> }) {
  const { path } = await context.params;
  const base = process.env.API_INTERNAL_URL ?? "http://127.0.0.1:8000";
  const url = new URL(path.map(encodeURIComponent).join("/") + request.nextUrl.search, base + "/");
  const headers = new Headers();
  for (const key of ["content-type", "origin", "sec-fetch-site", "x-yureatlas-request"]) {
    const value = request.headers.get(key);
    if (value) headers.set(key, value);
  }
  try {
    const upstream = await fetch(url, {
      method: request.method, headers, cache: "no-store",
      body: ["GET", "HEAD"].includes(request.method) ? undefined : await request.arrayBuffer(),
      signal: AbortSignal.timeout(30000),
    });
    const responseHeaders = new Headers({"content-type": upstream.headers.get("content-type") ?? "application/json"});
    const disposition = upstream.headers.get("content-disposition");
    if (disposition) responseHeaders.set("content-disposition", disposition);
    return new Response(upstream.body, {status: upstream.status, headers: responseHeaders});
  } catch {
    return Response.json({detail: "The local API is unavailable. Start the stack with make up."}, {status: 503});
  }
}
export { proxy as GET, proxy as POST };
