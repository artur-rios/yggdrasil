const http = require('node:http');

const server = http.createServer((request, response) => {
  if (request.url === '/healthz') {
    response.writeHead(200, { 'content-type': 'text/plain' });
    response.end('ok');
    return;
  }
  response.writeHead(200, { 'content-type': 'application/json' });
  response.end(JSON.stringify({ path: request.url, commit: process.env.GIT_COMMIT, environment: process.env.YGG_ENVIRONMENT }));
});

server.listen(Number(process.env.PORT || 8080), '0.0.0.0');
process.on('SIGTERM', () => server.close(() => process.exit(0)));
