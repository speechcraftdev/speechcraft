// Browser coverage for the onboarding workflow.
// Starts an isolated backend and the Next.js dev server, then drives Chromium.
// It does not read or write the installed Cozytent database or model cache.
//
// Run: node frontend/scripts/onboarding-e2e.mjs
// Requires the `playwright` package resolvable from NODE_PATH and a Chromium binary.

import { execFileSync, spawn } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { createRequire } from "node:module";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const frontendDir = path.join(root, "frontend");
const shots = path.join(frontendDir, "tmp", "onboarding-e2e");
const results = [];
let activePage = null;

function record(name, ok, notes) {
  results.push({ name, ok, notes });
  const line = `${ok ? "PASS" : "FAIL"}  ${name}${notes ? ` — ${notes}` : ""}`;
  console.log(line);
  try { writeFileSync(path.join(shots, "progress.log"), `${results.map((item) => `${item.ok ? "PASS" : "FAIL"}  ${item.name}${item.notes ? ` — ${item.notes}` : ""}`).join("\n")}\n`); } catch { /* directory may not exist yet */ }
}

function logStep(message) {
  console.log(message);
  try {
    mkdirSync(shots, { recursive: true });
    writeFileSync(path.join(shots, "step.log"), `${new Date().toISOString()} ${message}\n`, { flag: "a" });
  } catch { /* best-effort progress log */ }
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close(() => resolve(port));
    });
  });
}

function waitForHttp(url, child, timeoutMs) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const tick = async () => {
      if (child.exitCode != null) {
        reject(new Error(`${url} process exited (${child.exitCode})`));
        return;
      }
      try {
        const response = await fetch(url);
        if (response.ok || response.status === 404) {
          resolve();
          return;
        }
      } catch { /* still starting */ }
      if (Date.now() - started > timeoutMs) {
        reject(new Error(`Timed out waiting for ${url}`));
        return;
      }
      setTimeout(tick, 250);
    };
    tick();
  });
}

function wavBytes(samples = 1600) {
  const data = Buffer.alloc(samples * 2);
  for (let i = 0; i < samples; i += 1) data.writeInt16LE(Math.round(Math.sin(i / 8) * 1200), i * 2);
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + data.length, 4);
  header.write("WAVE", 8);
  header.write("fmt ", 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(16000, 24);
  header.writeUInt32LE(32000, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(data.length, 40);
  return Buffer.concat([header, data]);
}

function uploadIdentity(content) {
  const chunkHashes = [];
  for (let offset = 0; offset < content.length; offset += 8 * 1024 * 1024) {
    chunkHashes.push(createHash("sha256").update(content.subarray(offset, offset + 8 * 1024 * 1024)).digest("hex"));
  }
  const manifest = Buffer.alloc(8 + chunkHashes.length * 32);
  manifest.writeBigUInt64BE(BigInt(content.length), 0);
  chunkHashes.forEach((hex, index) => Buffer.from(hex, "hex").copy(manifest, 8 + index * 32));
  return { chunk_hashes: chunkHashes, file_identity_sha256: createHash("sha256").update(manifest).digest("hex") };
}

async function api(base, pathname, init) {
  const response = await fetch(`${base}${pathname}`, init);
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  return { status: response.status, body };
}

async function uploadWav(base, projectId, filename, content) {
  const session = await api(base, `/api/projects/${projectId}/source-recordings/upload-sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename, size: content.length, client_upload_id: randomBytes(16).toString("hex"), ...uploadIdentity(content) }),
  });
  if (session.status !== 200) throw new Error(`upload session ${session.status}: ${JSON.stringify(session.body)}`);
  const digest = createHash("sha256").update(content).digest("hex");
  const chunk = await api(base, `/api/projects/${projectId}/source-recordings/upload-sessions/${session.body.upload_id}/chunks/0`, {
    method: "PUT",
    headers: { "Content-Type": "application/octet-stream", "X-Chunk-SHA256": digest },
    body: content,
  });
  if (chunk.status !== 200) throw new Error(`upload chunk ${chunk.status}: ${JSON.stringify(chunk.body)}`);
  const finalized = await api(base, `/api/projects/${projectId}/source-recordings/upload-sessions/${session.body.upload_id}/finalize`, { method: "POST" });
  if (finalized.status !== 200) throw new Error(`finalize ${finalized.status}: ${JSON.stringify(finalized.body)}`);
  return finalized.body;
}

function setRunStateArgs(python, dbPath, runId, status, stage) {
  return new Promise((resolve, reject) => {
    const child = spawn(python, ["-c", `
import sqlite3, sys
db, run_id, status, stage = sys.argv[1:]
con = sqlite3.connect(db)
tables = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
table = next(name for name in tables if name.lower() == "processingrun")
cur = con.execute(f"UPDATE {table} SET status=?, stage=? WHERE id=?", (status, stage, run_id))
con.commit()
if cur.rowcount != 1:
    raise SystemExit(f"updated {cur.rowcount} rows")
print("ok")
`, dbPath, runId, status, stage], { stdio: ["ignore", "pipe", "pipe"] });
    let err = "";
    child.stderr.on("data", (chunk) => { err += chunk; });
    child.on("exit", (code) => code === 0 ? resolve() : reject(new Error(err.trim() || `sqlite update failed (${code})`)));
  });
}

function loadPlaywright() {
  const require = createRequire(import.meta.url);
  if (process.env.SPEECHCRAFT_PLAYWRIGHT) return require(process.env.SPEECHCRAFT_PLAYWRIGHT);
  const bin = execFileSync("which", ["playwright"], { encoding: "utf8" }).trim();
  const match = readFileSync(bin, "utf8").match(/target=(\S+)/);
  if (!match) throw new Error("Could not locate Playwright. Set SPEECHCRAFT_PLAYWRIGHT to its index.js.");
  return require(path.join(path.dirname(path.resolve(path.dirname(bin), match[1])), "index.js"));
}

async function main() {
  mkdirSync(shots, { recursive: true });
  writeFileSync(path.join(shots, "step.log"), "");
  logStep("loading playwright");
  const playwright = loadPlaywright();
  logStep("playwright loaded");
  const work = path.join(tmpdir(), `speechcraft-onboarding-e2e-${process.pid}`);
  mkdirSync(work, { recursive: true });
  const dbPath = path.join(work, "project.db");
  const backendPort = Number(process.env.SPEECHCRAFT_E2E_BACKEND_PORT) || await freePort();
  const frontendPort = Number(process.env.SPEECHCRAFT_E2E_FRONTEND_PORT) || await freePort();
  const deadControllerPort = Number(process.env.SPEECHCRAFT_E2E_CONTROLLER_PORT) || await freePort();
  const backend = `http://127.0.0.1:${backendPort}`;
  const app = `http://127.0.0.1:${frontendPort}`;
  const python = path.join(root, "backend", ".venv", "bin", "python");
  const backendProc = spawn(python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(backendPort)], {
    cwd: path.join(root, "backend"),
    env: {
      ...process.env,
      PYTHONPATH: path.join(root, "backend"),
      SPEECHCRAFT_DB_PATH: dbPath,
      SPEECHCRAFT_LEGACY_SEED_PATH: path.join(work, "missing-seed.json"),
      SPEECHCRAFT_MEDIA_ROOT: path.join(work, "media"),
      SPEECHCRAFT_EXPORTS_ROOT: path.join(work, "exports"),
      SPEECHCRAFT_ALLOWED_ORIGINS: app,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const frontendProc = spawn(path.join(frontendDir, "node_modules", ".bin", "next"), ["start", "--hostname", "127.0.0.1", "-p", String(frontendPort)], {
    cwd: frontendDir,
    env: {
      ...process.env,
      SPEECHCRAFT_BACKEND_URL: backend,
      SPEECHCRAFT_SETUP_CONTROLLER_URL: `http://127.0.0.1:${deadControllerPort}`,
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let backendLog = "";
  let frontendLog = "";
  backendProc.stdout.on("data", (chunk) => { backendLog += chunk; });
  backendProc.stderr.on("data", (chunk) => { backendLog += chunk; });
  frontendProc.stdout.on("data", (chunk) => { frontendLog += chunk; });
  frontendProc.stderr.on("data", (chunk) => { frontendLog += chunk; });
  const stop = () => {
    for (const child of [frontendProc, backendProc]) {
      if (child.exitCode == null) child.kill("SIGTERM");
    }
  };
  process.on("exit", stop);

  try {
    logStep(`waiting for backend ${backend}`);
    await waitForHttp(`${backend}/healthz`, backendProc, 30000);
    logStep(`waiting for frontend ${app}`);
    await waitForHttp(app, frontendProc, 90000);
    logStep("launching chromium");
    const browser = await playwright.chromium.launch({
      executablePath: process.env.SPEECHCRAFT_E2E_CHROMIUM || "/usr/bin/chromium",
      headless: true,
      args: ["--no-sandbox", "--disable-dev-shm-usage"],
    });
    try {
      await runSuite({ browser, app, backend, dbPath, python });
    } finally {
      await browser.close();
    }
  } finally {
    stop();
    writeFileSync(path.join(shots, "backend.log"), backendLog);
    writeFileSync(path.join(shots, "frontend.log"), frontendLog);
  }

  const failed = results.filter((item) => !item.ok);
  console.log(`\n${results.length - failed.length}/${results.length} browser scenarios passed`);
  console.log(`Screenshots and logs: ${shots}`);
  if (failed.length) process.exitCode = 1;
}

async function runSuite({ browser, app, backend, dbPath, python }) {
  const pageErrors = [];
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const page = await context.newPage();
  activePage = page;
  page.on("pageerror", (error) => pageErrors.push(error.message));
  const stamp = Date.now().toString(36);

  await scenario("root opens onboarding welcome", async () => {
    await page.goto(app, { waitUntil: "domcontentloaded" });
    await page.getByRole("heading", { name: "Train better voice models." }).waitFor();
    if (!page.url().includes("/onboarding/welcome")) throw new Error(page.url());
    await page.screenshot({ path: path.join(shots, "01-welcome-desktop.png") });
  });

  await scenario("welcome continue opens setup and does not start a download", async () => {
    const posts = [];
    page.on("request", (request) => {
      if (request.method() === "POST") posts.push(new URL(request.url()).pathname);
    });
    await page.getByRole("button", { name: "Continue" }).click();
    await page.waitForURL(/\/onboarding\/setup/, { timeout: 15000 });
    await page.getByRole("heading").first().waitFor();
    const deadline = Date.now() + 25000;
    while (Date.now() < deadline) {
      const heading = await page.getByRole("heading").first().innerText();
      if (heading !== "Checking Speechcraft") break;
      await page.waitForTimeout(250);
    }
    const heading = await page.getByRole("heading").first().innerText();
    const downloadPosts = posts.filter((pathname) => /ensure|repair|download|import-batches/i.test(pathname));
    if (downloadPosts.length) throw new Error(`unexpected posts: ${downloadPosts.join(", ")}`);
    await page.screenshot({ path: path.join(shots, "02-setup.png") });
    return `stable heading: ${heading}`;
  });

  await scenario("legacy routes redirect once and keep query values", async () => {
    await page.goto(`${app}/login5?project=project%20one&run=run-1&demo=1`, { waitUntil: "domcontentloaded" });
    await page.waitForURL(/\/onboarding\/processing/, { timeout: 15000 });
    const url = new URL(page.url());
    if (url.searchParams.get("project") !== "project one" || url.searchParams.get("run") !== "run-1" || url.searchParams.get("demo") !== "1") {
      throw new Error(url.toString());
    }
    await page.goto(`${app}/login2?project=kept-project`, { waitUntil: "domcontentloaded" });
    await page.waitForURL(/\/onboarding\/import/, { timeout: 15000 });
    if (!page.url().includes("project=kept-project")) throw new Error(page.url());
    await page.goto(`${app}/login3`, { waitUntil: "domcontentloaded" });
    await page.waitForURL(/\/onboarding\/setup/, { timeout: 15000 });
    if (page.url().includes("/login2") || page.url().includes("/login3")) throw new Error(page.url());
  });

  const projectId = `e2e-${stamp}`;
  await scenario("valid upload, back to import, and a second recording stay in one project", async () => {
    await page.goto(`${app}/onboarding/import`, { waitUntil: "domcontentloaded" });
    const first = path.join(shots, "speaker-a.wav");
    const second = path.join(shots, "speaker-b.wav");
    const firstBytes = wavBytes(1800);
    const secondBytes = wavBytes(2200);
    writeFileSync(first, firstBytes);
    writeFileSync(second, secondBytes);
    await page.locator("#upload-files").setInputFiles(first);
    await page.getByRole("heading", { name: "How many speakers are in your recordings?" }).waitFor({ timeout: 30000 });
    const speakersUrl = new URL(page.url());
    const created = speakersUrl.searchParams.get("project");
    if (!created) throw new Error("speaker page lost the project");
    await page.screenshot({ path: path.join(shots, "03-speakers.png") });
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.getByRole("heading", { name: "How many speakers are in your recordings?" }).waitFor();
    if (new URL(page.url()).searchParams.get("project") !== created) throw new Error("refresh dropped the project");
    await page.getByRole("button", { name: "Back to import" }).click();
    await page.waitForURL(new RegExp(`/onboarding/import\\?project=${created}`));
    await page.getByText(`Adding recordings to`).waitFor();
    await page.locator("#upload-files").setInputFiles(second);
    await page.getByRole("heading", { name: "How many speakers are in your recordings?" }).waitFor({ timeout: 30000 });
    if (new URL(page.url()).searchParams.get("project") !== created) throw new Error("second upload changed the project");
    const projects = await api(backend, "/api/projects");
    const matches = projects.body.filter((item) => item.id === created);
    if (matches.length !== 1) throw new Error(`project count ${matches.length}`);
    const recordings = await api(backend, `/api/projects/${created}/source-recordings`);
    if (recordings.body.length !== 2) throw new Error(`recordings ${recordings.body.length}`);
    const ids = new Set(recordings.body.map((item) => item.id));
    if (ids.size !== 2) throw new Error("a recording was overwritten");
    await page.screenshot({ path: path.join(shots, "04-speakers-after-second-upload.png") });
    return created;
  });

  await scenario("speaker choice is required and a double click does not create two runs", async () => {
    const project = new URL(page.url()).searchParams.get("project");
    const continueButton = page.getByRole("button", { name: "Continue" });
    if (await continueButton.isEnabled()) throw new Error("Continue started enabled");
    const one = page.getByRole("radio", { name: /One speaker/ });
    const many = page.getByRole("radio", { name: /Multiple speakers/ });
    if (await one.getAttribute("aria-checked") !== "false" || await many.getAttribute("aria-checked") !== "false") {
      throw new Error("a speaker option was selected by default");
    }
    await many.click();
    await one.click();
    if (await one.getAttribute("aria-checked") !== "true") throw new Error("selection did not change");
    const posts = [];
    const onRequest = (request) => {
      if (request.method() === "POST" && request.url().includes("/dataset-runs")) posts.push(request.url());
    };
    page.on("request", onRequest);
    await continueButton.click();
    await continueButton.click({ timeout: 1000 }).catch(() => {});
    await Promise.race([
      page.waitForURL(/\/onboarding\/(processing|choose-speaker)/, { timeout: 45000 }),
      page.getByRole("alert").waitFor({ timeout: 45000 }),
    ]).catch(() => {});
    page.off("request", onRequest);
    const runs = await api(backend, `/api/projects/${project}/dataset-runs`);
    if (!Array.isArray(runs.body)) throw new Error(JSON.stringify(runs.body));
    if (runs.body.length > 1) throw new Error(`created ${runs.body.length} runs`);
    if (posts.length > 1) throw new Error(`posted ${posts.length} dataset-run creates`);
    return runs.body.length === 1 ? `one run ${runs.body[0].id} single_speaker=${runs.body[0].input_summary.single_speaker}` : `no run created yet (${posts.length} posts)`;
  });

  await scenario("an invalid project link does not create a replacement project", async () => {
    const before = new Set((await api(backend, "/api/projects")).body.map((item) => item.id));
    await page.goto(`${app}/onboarding/import?project=missing-${stamp}`, { waitUntil: "domcontentloaded" });
    await page.getByRole("heading", { name: "This project is not available" }).waitFor();
    await page.getByText("The import link does not match a project on this Speechcraft installation.").waitFor();
    await page.screenshot({ path: path.join(shots, "05-invalid-project.png") });
    const after = new Set((await api(backend, "/api/projects")).body.map((item) => item.id));
    if ([...after].some((id) => !before.has(id))) throw new Error("a project was created for an invalid link");
    await page.getByRole("button", { name: "Start a new import" }).click();
    await page.waitForURL(/\/onboarding\/import$/);
  });

  await scenario("unsupported and empty uploads do not create recordings", async () => {
    const project = `reject-${stamp}`;
    const created = await api(backend, "/api/import-batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: project, name: `Reject ${stamp}` }),
    });
    if (created.status !== 200) throw new Error(JSON.stringify(created.body));
    await page.goto(`${app}/onboarding/import?project=${project}`, { waitUntil: "domcontentloaded" });
    const bogus = path.join(shots, "notes.txt");
    writeFileSync(bogus, "not audio");
    await page.locator("#upload-files").setInputFiles(bogus);
    await page.waitForTimeout(1000);
    const recordings = await api(backend, `/api/projects/${project}/source-recordings`);
    if (recordings.body.length !== 0) throw new Error("unsupported file became a recording");
    await page.getByRole("button", { name: "Upload" }).click();
    await page.waitForTimeout(300);
    if ((await api(backend, `/api/projects/${project}/source-recordings`)).body.length !== 0) {
      throw new Error("empty submit created a recording");
    }
  });

  await scenario("a completed speaker_purity run opens the lab without restarting", async () => {
    const project = `done-${stamp}`;
    await api(backend, "/api/import-batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: project, name: `Done ${stamp}` }),
    });
    const recording = await uploadWav(backend, project, "done.wav", wavBytes());
    const run = await api(backend, `/api/projects/${project}/dataset-runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_recording_ids: [recording.id],
        wizard_attempt: `onboarding:${project}:done`,
        single_speaker: true,
        whisper_model_size: "base",
        language: "auto",
        stop_after: "candidate_review_clips",
        target_speaker_label: "speaker_0",
      }),
    });
    if (run.status !== 201) throw new Error(JSON.stringify(run.body));
    await setRunStateArgs(python, dbPath, run.body.id, "completed", "speaker_purity");
    const confirmed = await api(backend, `/api/dataset-runs/${run.body.id}`);
    if (confirmed.body.status !== "completed" || confirmed.body.stage !== "speaker_purity") {
      throw new Error(JSON.stringify(confirmed.body));
    }
    const methods = [];
    const onRequest = (request) => {
      const pathname = new URL(request.url()).pathname;
      if (/\/start$|resume-processing|qc\/generate/.test(pathname)) methods.push(`${request.method()} ${pathname}`);
    };
    page.on("request", onRequest);
    await page.goto(`${app}/onboarding/processing?project=${project}&run=${run.body.id}`, { waitUntil: "domcontentloaded" });
    await page.waitForURL(/\/lab\?/, { timeout: 20000 });
    const landed = new URL(page.url());
    if (landed.searchParams.get("run") !== run.body.id || landed.searchParams.get("project") !== project) {
      throw new Error(landed.toString());
    }
    await page.waitForTimeout(1000);
    page.off("request", onRequest);
    if (methods.length) throw new Error(`restarted work: ${methods.join(", ")}`);
    const again = await api(backend, `/api/dataset-runs/${run.body.id}`);
    if (again.body.status !== "completed" || again.body.id !== run.body.id) throw new Error("run identity changed");
    await page.screenshot({ path: path.join(shots, "06-lab-after-completed-run.png") });
  });

  await scenario("a failed run shows recovery instead of waiting forever", async () => {
    const project = `failed-${stamp}`;
    await api(backend, "/api/import-batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: project, name: `Failed ${stamp}` }),
    });
    const recording = await uploadWav(backend, project, "failed.wav", wavBytes(800));
    const run = await api(backend, `/api/projects/${project}/dataset-runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source_recording_ids: [recording.id],
        wizard_attempt: `onboarding:${project}:failed`,
        single_speaker: false,
        whisper_model_size: "base",
      }),
    });
    if (run.status !== 201) throw new Error(JSON.stringify(run.body));
    await setRunStateArgs(python, dbPath, run.body.id, "failed", "speaker_purity");
    await page.goto(`${app}/onboarding/processing?project=${project}&run=${run.body.id}`, { waitUntil: "domcontentloaded" });
    await page.getByText("This dataset run failed.").waitFor({ timeout: 15000 });
    await page.getByRole("button", { name: "Return to speaker mode" }).waitFor();
    if (page.url().includes("/lab")) throw new Error("failed run was treated as reviewable");
    await page.screenshot({ path: path.join(shots, "07-failed-run.png") });
  });

  await scenario("a run from another project is rejected", async () => {
    const donor = (await api(backend, `/api/projects/done-${stamp}/dataset-runs`)).body[0];
    if (!donor) throw new Error("missing donor run");
    await page.goto(`${app}/onboarding/processing?project=failed-${stamp}&run=${donor.id}`, { waitUntil: "domcontentloaded" });
    await page.getByText("different project").waitFor({ timeout: 15000 });
  });

  await scenario("demo mode reaches review without creating a dataset run", async () => {
    const posts = [];
    const onRequest = (request) => {
      if (request.method() === "POST" && request.url().includes("/dataset-runs")) posts.push(request.url());
    };
    page.on("request", onRequest);
    await page.goto(`${app}/onboarding/welcome?demo=1`, { waitUntil: "domcontentloaded" });
    await page.getByRole("button", { name: "Continue" }).click();
    await page.waitForURL(/\/onboarding\/import/, { timeout: 15000 });
    if (!page.url().includes("demo=1")) throw new Error(page.url());
    await page.getByRole("button", { name: "Continue with current project" }).click();
    await page.getByRole("heading", { name: "How many speakers are in your recordings?" }).waitFor();
    await page.getByRole("radio", { name: /One speaker/ }).click();
    await page.getByRole("button", { name: "Continue" }).click();
    await page.waitForURL(/\/onboarding\/processing/, { timeout: 15000 });
    await page.getByText("Demo — simulated, no processing running").waitFor();
    await page.waitForURL(/\/lab\?/, { timeout: 15000 });
    if (!page.url().includes("demo=1")) throw new Error("demo flag was dropped");
    page.off("request", onRequest);
    if (posts.length) throw new Error(`demo created a run: ${posts.join(", ")}`);
    await page.screenshot({ path: path.join(shots, "08-demo-lab.png") });
  });

  await scenario("mobile welcome does not scroll sideways", async () => {
    const mobile = await browser.newContext({ viewport: { width: 390, height: 844 } });
    const mobilePage = await mobile.newPage();
    await mobilePage.goto(`${app}/onboarding/welcome`, { waitUntil: "domcontentloaded" });
    await mobilePage.getByRole("heading", { name: "Train better voice models." }).waitFor();
    const overflow = await mobilePage.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    await mobilePage.screenshot({ path: path.join(shots, "09-welcome-mobile.png") });
    await mobile.close();
    if (overflow > 1) throw new Error(`horizontal overflow ${overflow}px`);
  });

  if (pageErrors.length) {
    record("no unexpected page errors", false, pageErrors.slice(0, 5).join(" | "));
  } else {
    record("no unexpected page errors", true, "");
  }
  await context.close();
}

async function scenario(name, fn) {
  try {
    const notes = await fn();
    record(name, true, typeof notes === "string" ? notes : "");
  } catch (error) {
    if (activePage) {
      await activePage.screenshot({ path: path.join(shots, `fail-${results.length + 1}.png`) }).catch(() => {});
    }
    record(name, false, error instanceof Error ? error.message : String(error));
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
