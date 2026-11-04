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
      if (session?.parentID) output.headers["X-Proxi-Parent-Session-Id"] = session.parentID
      if (typeof session?.title === "string") {
        try {
          // HTTP/1 headers must be ASCII, including Russian and emoji chat names.
          // Cut by code points: slice() can split a surrogate pair and make encodeURIComponent throw.
          output.headers["X-Proxi-Chat-Title"] = encodeURIComponent(Array.from(session.title).slice(0, 240).join(""))
        } catch {
          // A title that cannot be encoded is skipped; the parent link above is already set.
        }
      }
    } catch {
      // The relay can also read the local session DB; a lookup must not fail a turn.
    }
  },
})
