export const dynamic = 'force-dynamic';

export default async function Post({ params }) {
  const { slug } = await params;
  return <main><h1>post {slug}</h1></main>;
}
