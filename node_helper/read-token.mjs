/**
 * Reads the Plaud authToken from LevelDB using a proper parser.
 * Copies the DB to a temp dir to avoid locking issues with the running app.
 * Outputs the token to stdout (or exits with code 1 on failure).
 */
import { ClassicLevel } from 'classic-level';
import { mkdtempSync, cpSync, rmSync, unlinkSync, existsSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

const LEVELDB_DIR = join(process.env.HOME, 'Library/Application Support/Plaud/Local Storage/leveldb');
const tmpDir = mkdtempSync(join(tmpdir(), 'plaud-leveldb-'));

try {
  cpSync(LEVELDB_DIR, tmpDir, { recursive: true });
  const lockFile = join(tmpDir, 'LOCK');
  if (existsSync(lockFile)) unlinkSync(lockFile);

  const db = new ClassicLevel(tmpDir, { createIfMissing: false });
  await db.open();

  let token = null;
  const iter = db.iterator();
  try {
    for await (const [key, value] of iter) {
      const valStr = value.toString();
      const bearerIdx = valStr.indexOf('bearer ');
      if (bearerIdx >= 0) {
        token = valStr.substring(bearerIdx);
        break;
      }
    }
  } finally {
    await iter.close();
    await db.close();
  }

  if (token) {
    process.stdout.write(token);
  } else {
    process.stderr.write('No auth token found in LevelDB\n');
    process.exit(1);
  }
} finally {
  rmSync(tmpDir, { recursive: true, force: true });
}
