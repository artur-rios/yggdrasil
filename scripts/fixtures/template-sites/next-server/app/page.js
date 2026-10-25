// Rendered per request, so it reads the container's environment, not the build's.
export const dynamic = 'force-dynamic';

export default function Page() {
  return (
    <main>
      <h1>next server sample</h1>
      <p>commit {process.env.GIT_COMMIT} in {process.env.YGG_ENVIRONMENT}</p>
    </main>
  );
}
