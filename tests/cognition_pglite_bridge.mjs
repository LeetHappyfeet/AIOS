import readline from 'node:readline';
const {PGlite} = await import(process.env.AIOS_PGLITE_MODULE);
const db = new PGlite();
const input = readline.createInterface({input: process.stdin});
for await (const line of input) {
  try {
    const request = JSON.parse(line);
    const value = request.exec ? await db.exec(request.sql) : await db.query(request.sql, request.args || []);
    process.stdout.write(JSON.stringify({value}) + '\n');
  } catch (error) {
    process.stdout.write(JSON.stringify({error: error.message}) + '\n');
  }
}
await db.close();
