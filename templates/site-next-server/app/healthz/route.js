// /healthz for the status API and the container's HEALTHCHECK: 200 while the server can answer. Copy it to
// app/healthz/route.js of the Next.js app (src/app/healthz/route.js when the app uses src/).
//
// Static on purpose: it checks that the process serves requests, not that the app's dependencies are up, so a
// slow database does not make Docker restart the container. (`basePath` would move it under that path; the
// stacks' health check expects /healthz at the root, so a site served at a host's root has none.)
export const dynamic = 'force-static';

export function GET() {
  return new Response('ok', { status: 200, headers: { 'content-type': 'text/plain' } });
}
