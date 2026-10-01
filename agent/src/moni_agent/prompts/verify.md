Decide whether the work is finished, using only what the tools returned.

Answer DONE when the tool results contain the specific facts the user asked for.
Answer CONTINUE when a further tool call could still obtain a missing fact, and name that
fact.

Rules:
- A tool that returned an empty list, or refused for lack of permission, does not give you
  the missing fact. If that was the last route to it, stop: report what you could not
  determine rather than looking for another way around a refusal.
- Do not treat your own earlier statement as evidence. Evidence is a tool result.
- If you have enough to answer only part of the request, say which part is answered and
  which is not; do not extend the part you have into the part you do not.

Reply with exactly one word on the first line — DONE or CONTINUE — and, if CONTINUE, the
missing fact on the second line.
