export default function Page() {
  return (
    <main>
      <h1>next static sample</h1>
      <p>commit {process.env.NEXT_PUBLIC_GIT_COMMIT} in {process.env.NEXT_PUBLIC_YGG_ENVIRONMENT}</p>
    </main>
  );
}
