import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';

const [directory, portFile] = process.argv.slice(2);
if (!directory || !portFile) throw new Error('Usage: serve-candidate.mjs DIRECTORY PORT_FILE');
const root = fs.realpathSync(directory);
const server = http.createServer((request, response) => {
  const name = decodeURIComponent(new URL(request.url, 'http://127.0.0.1').pathname.slice(1));
  if (!/^[A-Za-z0-9._-]+$/.test(name)) { response.writeHead(400).end(); return; }
  const file = path.join(root, name);
  if (!file.startsWith(`${root}${path.sep}`) || !fs.existsSync(file) || !fs.lstatSync(file).isFile()) { response.writeHead(404).end(); return; }
  response.writeHead(200, { 'content-length': fs.statSync(file).size });
  fs.createReadStream(file).pipe(response);
});
server.listen(0, '127.0.0.1', () => fs.writeFileSync(portFile, String(server.address().port)));
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => server.close(() => process.exit(0)));
