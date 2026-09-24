// Route A autonomous drafter — Sneaker Station store Gmail (sneakerstationisrael@) via Chrome.
// Draft-only. Never sends. Hard guard: aborts unless the active Gmail account is the store.
// No API key needed. Safe templated replies for shipping/status questions only.
// Anything sensitive (refund / exchange / complaint / legal) is left untouched for Itay.

import { execFileSync } from "node:child_process";

const STORE_EMAIL = "sneakerstationisrael@gmail.com";
const ACCOUNT_INDEX = 4; // u/4 — verified by title guard below, not trusted blindly.
const BASE = `https://mail.google.com/mail/u/${ACCOUNT_INDEX}/#`;

// Senders that are notifications/marketing, never a customer to reply to.
const IGNORE_SENDER = /@(t\.shopifyemail\.com|shopify\.com|email\.shopify\.com|notifications\.tiktok\.com|jotform\.com|email\.shopify\.com)$/i;

// A real customer status question we are confident to auto-draft.
const STATUS_Q = /(איפה|מתי|הזמנה|משלוח|הגיע|הגייע|טראקינג|מעקב|order|where|when|tracking|shipping)/i;

// Sensitive — never auto-draft, leave for Itay.
const SENSITIVE = /(החלפ|החזר|ביטול|פגום|שבור|תלונה|כסף בחזרה|refund|return|exchange|cancel|broken|complaint|לקוי|תביע|עורך דין|משפט)/i;

function osa(js) {
  // Run JS in the active tab of the front Chrome window.
  const wrapped = `tell application "Google Chrome" to execute (active tab of window 1) javascript ${JSON.stringify(js)}`;
  return execFileSync("osascript", ["-e", wrapped], { encoding: "utf8" }).trim();
}
function osaPlain(applescript) {
  return execFileSync("osascript", ["-e", applescript], { encoding: "utf8" }).trim();
}
const sleep = (ms) => execFileSync("sleep", [String(ms / 1000)]);

function execJsEnabled() {
  try { return osa("1+1") === "2"; } catch { return false; }
}

function setUrl(hash) {
  osaPlain(`tell application "Google Chrome" to set URL of active tab of window 1 to "${BASE}${hash}"`);
}
function activate() { osaPlain('tell application "Google Chrome" to activate'); }

function currentTitle() {
  try { return osa("document.title"); } catch { return ""; }
}

// Hebrew-safe: pass text as base64, decode in page.
function b64(s) { return Buffer.from(s, "utf8").toString("base64"); }

function readInbox() {
  const js = `(function(){
    var rows=document.querySelectorAll('tr.zA');var out=[];
    rows.forEach(function(r){
      var s=r.querySelector('.yW span[email]');var from=s?s.getAttribute('email'):'';
      var subjEl=r.querySelector('.bog');var subj=subjEl?subjEl.innerText:'';
      var unread=r.classList.contains('zE');
      var hasDraft=/\\u05d8\\u05d9\\u05d5\\u05d8\\u05d4/.test(r.innerText); // "טיוטה"
      if(subj) out.push({from:from,subj:subj,unread:unread,hasDraft:hasDraft});
    });
    return JSON.stringify(out);
  })();`;
  try { return JSON.parse(osa(js)); } catch { return []; }
}

// Open a thread by clicking its row (matched by sender), return permalink hash or "".
function openThreadBySender(from) {
  const js = `(function(em){var rows=document.querySelectorAll('tr.zA');for(var i=0;i<rows.length;i++){var s=rows[i].querySelector('.yW span[email]');if(s&&s.getAttribute('em'.concat('ail'))===em){(rows[i].querySelector('.bog')||rows[i]).click();return 'ok';}}return 'nf';})(${JSON.stringify(from)});`;
  if (osa(js) !== "ok") return "";
  sleep(3000);
  let hash = "";
  try { hash = osa("location.hash"); } catch { hash = ""; }
  return hash.startsWith("#inbox/") ? hash.slice(1) : "";
}

function draftReplyInOpenThread(text) {
  // open reply editor
  const clicked = osa("(function(){var e=document.querySelector('span.ams.bkH');if(!e)return 'no-btn';e.click();return 'clicked';})()");
  if (clicked !== "clicked") return "no-reply-button";
  // poll for editor
  let ok = false;
  for (let k = 0; k < 6; k++) {
    sleep(2000);
    if (osa("document.querySelectorAll('[contenteditable=true]').length") !== "0") { ok = true; break; }
  }
  if (!ok) return "no-editor";
  const setJs = `(function(){try{
    var ed=document.querySelector('[contenteditable=true]');if(!ed)return 'no-editor';
    var txt=decodeURIComponent(escape(atob(${JSON.stringify(b64(text))})));
    var lines=txt.split(String.fromCharCode(10));
    while(ed.firstChild) ed.removeChild(ed.firstChild);
    for(var i=0;i<lines.length;i++){var d=document.createElement('div');d.textContent=lines[i]||String.fromCharCode(160);ed.appendChild(d);}
    ed.dispatchEvent(new InputEvent('input',{bubbles:true}));
    return 'OK '+ed.innerText.length;
  }catch(e){return 'ERR:'+e.message;}})();`;
  const r = osa(setJs);
  sleep(6000); // let Gmail autosave the draft
  return r;
}

function buildReply() {
  return [
    "היי,",
    "תודה על הסבלנות וסליחה על ההמתנה.",
    "בחודשים האחרונים היו עיכובים במשלוחים עקב המצב הביטחוני בארץ. המצב נפתר והמשלוחים שוב יוצאים במהירות, וההזמנה שלך בדרך אליך ואמורה להגיע בימים הקרובים. נשלח לך מספר מעקב ברגע שהיא יוצאת.",
    "אם תרצה שנבדוק משהו ספציפי בהזמנה, פשוט תגיב כאן ונעזור.",
    "תודה שבחרת בנו,",
    "SneakerStation",
  ].join("\n");
}

function main() {
  const log = (...a) => console.log(new Date().toISOString(), ...a);

  if (!execJsEnabled()) {
    log("ABORT: execute-js disabled. Fix Preferences + restart Chrome to heal.");
    process.exit(2);
  }
  activate(); sleep(800);
  setUrl("inbox"); sleep(4000); activate(); sleep(1500);

  const title = currentTitle();
  if (!title.includes(STORE_EMAIL)) {
    log(`ABORT (account guard): active account is not the store. title="${title}"`);
    process.exit(3);
  }
  log("Account guard OK:", title);

  const inbox = readInbox();
  const candidates = inbox.filter(
    (m) => m.from && !IGNORE_SENDER.test(m.from) && !m.hasDraft && !SENSITIVE.test(m.subj) && STATUS_Q.test(m.subj)
  );
  log(`inbox=${inbox.length} candidates=${candidates.length}`);

  let drafted = 0;
  for (const m of candidates) {
    // re-assert account before each action
    setUrl("inbox"); sleep(3000); activate(); sleep(800);
    if (!currentTitle().includes(STORE_EMAIL)) { log("guard tripped mid-run, stop"); break; }
    const hash = openThreadBySender(m.from);
    if (!hash) { log("skip (no thread):", m.from); continue; }
    setUrl(hash.slice(1)); sleep(4000); activate(); sleep(1200); // stabilize on permalink
    if (!currentTitle().includes(STORE_EMAIL)) { log("guard tripped in thread, stop"); break; }
    const res = draftReplyInOpenThread(buildReply());
    log("draft", m.from, "=>", res);
    if (res.startsWith("OK")) drafted++;
  }
  log(`DONE. drafted=${drafted}`);
}

main();
