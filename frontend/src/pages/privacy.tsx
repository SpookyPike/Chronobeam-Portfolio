/** Privacy / data-protection notice — the stable route required by
 *  DP-RIGHTS-003 (DP-RIGHTS-003) and
 *  FE-PRIV-006 (FE-PRIV-006).
 *
 *  Engineering owns the ROUTE and COMPONENT only. The legal wording is owned
 *  by the organisation and its DPO, so every section below is an explicit
 *  placeholder naming what the approved notice must cover — no purposes,
 *  recipients, retention periods, legal bases or contact details are asserted
 *  here, and none may be invented to fill the page. The production-pilot gate
 *  (§14) is satisfied by the approved text replacing these placeholders, not by
 *  this component. */

/** Every topic is required to carry this marker verbatim so a reader (and the
 *  test) can tell approved content from placeholder content. It is stated ONCE
 *  for the whole list — repeating it per topic was seven statements of one
 *  fact. */
const PENDING = "Pending DPO approval";

/** The topics the approved notice must cover. Each entry is a plain-language
 *  statement of what will be said there, never the wording itself. */
const TOPICS: Array<{ title: string; willState: string }> = [
  {
    title: "Who is responsible",
    willState: "The controller for this processing, and how to reach the Data Protection Officer.",
  },
  {
    title: "Why your data is processed",
    willState: "The purposes for which working-time and related data are processed.",
  },
  {
    title: "What data is processed",
    willState: "The categories of personal data that are processed.",
  },
  {
    title: "Who can receive your data",
    willState:
      "The recipients, or categories of recipient, and the scopes under which they may see data.",
  },
  {
    title: "How long data is kept",
    willState: "The retention periods, and the reason for each of them.",
  },
  {
    title: "Your rights",
    willState: "Which data-protection rights apply, and how to exercise them.",
  },
  {
    title: "Contact",
    willState: "Where to raise a data-protection question or request.",
  },
];

export function PrivacyPage() {
  return (
    <section className="privacy-page" aria-label="Privacy notice">
      <p className="eyebrow">Data protection</p>
      <h1>Privacy notice</h1>
      <span className="private-badge">Text pending DPO approval</span>
      <p className="private-note">
        This notice is being finalised with the organisation’s Data Protection Officer. The topics
        below show what it will cover.
      </p>
      <section className="card">
        <p className="status-muted">
          {PENDING} — every line below is a placeholder for the wording the approved notice will
          carry.
        </p>
        <ul className="privacy-topics">
          {TOPICS.map((topic) => (
            <li key={topic.title}>
              <h2>{topic.title}</h2>
              <p className="status-muted">{topic.willState}</p>
            </li>
          ))}
        </ul>
      </section>
      {/* Who wrote the software, and who answers for the processing — the two
       * are deliberately not the same, so the attribution never reads as a
       * controller classification. */}
      <p className="private-note">
        Chronobeam was developed by <strong>Dmitrii Bespalov</strong>. Questions about this notice
        go to the organisation’s Data Protection Officer.
      </p>
    </section>
  );
}
