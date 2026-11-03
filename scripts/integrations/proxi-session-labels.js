/** Send the calling session's identity and local chat names to Proxi. */
export const ProxiSessionLabels = async ({ client }) => ({
  "chat.headers": async (input, output) => {
    if (input.model.providerID !== "retool-relay") return
    output.headers["X-Proxi-Client"] = "opencode"
    output.headers["X-Proxi-Session-Id"] = input.sessionID
    output.headers["X-Proxi-Agent"] = input.agent
    try {
      const result = await client.session.get({ path: { id: input.sessionID } })
      const session = result.data
      if (typeof session?.title === "string") {
        // HTTP/1 headers must be ASCII, including Russian and emoji chat names.
        output.headers["X-Proxi-Chat-Title"] = encodeURIComponent(session.title.slice(0, 240))
      }
      if (session?.parentID) output.headers["X-Proxi-Parent-Session-Id"] = session.parentID
    } catch {
      // The relay can also read the local session DB; a lookup must not fail a turn.
    }
  },
})
