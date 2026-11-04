import assert from "node:assert/strict"
import test from "node:test"
import { ProxiSessionLabels } from "../scripts/integrations/proxi-session-labels.js"

test("Unicode titles, renames and parent links are labels only", async () => {
  let title = "Название чата"
  const hooks = await ProxiSessionLabels({ client: { session: { get: async () => ({ data: { title, parentID: "ses_parent" } }) } } })
  const input = { model: { providerID: "retool-relay" }, sessionID: "ses_child", agent: "explore" }
  const output = { headers: {} }
  await hooks["chat.headers"](input, output)
  assert.equal(decodeURIComponent(output.headers["X-Proxi-Chat-Title"]), title)
  assert.equal(output.headers["X-Proxi-Parent-Session-Id"], "ses_parent")
  assert.equal(output.headers["X-Proxi-Agent"], "explore")
  assert.ok(!Object.keys(output.headers).some(key => /affinity|^x-session-id$/i.test(key)))
  title = "Новое имя"
  await hooks["chat.headers"](input, output)
  assert.equal(decodeURIComponent(output.headers["X-Proxi-Chat-Title"]), title)
})

test("A title cut inside an emoji keeps the whole title and the parent link", async () => {
  const title = "x".repeat(239) + "😀" + "tail"
  const hooks = await ProxiSessionLabels({ client: { session: { get: async () => ({ data: { title, parentID: "ses_parent" } }) } } })
  const output = { headers: {} }
  await hooks["chat.headers"]({ model: { providerID: "retool-relay" }, sessionID: "ses_child", agent: "build" }, output)
  assert.equal(decodeURIComponent(output.headers["X-Proxi-Chat-Title"]), "x".repeat(239) + "😀")
  assert.equal(output.headers["X-Proxi-Parent-Session-Id"], "ses_parent")
})

test("An unencodable title does not lose the parent link", async () => {
  const hooks = await ProxiSessionLabels({ client: { session: { get: async () => ({ data: { title: "ok\ud800", parentID: "ses_parent" } }) } } })
  const output = { headers: {} }
  await hooks["chat.headers"]({ model: { providerID: "retool-relay" }, sessionID: "ses_child", agent: "build" }, output)
  assert.equal(output.headers["X-Proxi-Parent-Session-Id"], "ses_parent")
  assert.equal(output.headers["X-Proxi-Chat-Title"], undefined)
})

test("Other providers are untouched; failed lookups keep request working", async () => {
  const hooks = await ProxiSessionLabels({ client: { session: { get: async () => { throw Error("offline") } } } })
  const output = { headers: {} }
  await hooks["chat.headers"]({ model: { providerID: "other" } }, output)
  assert.deepEqual(output.headers, {})
  await hooks["chat.headers"]({ model: { providerID: "retool-relay" }, sessionID: "ses_1", agent: "build" }, output)
  assert.equal(output.headers["X-Proxi-Session-Id"], "ses_1")
})
