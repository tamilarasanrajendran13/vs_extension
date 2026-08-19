Issue: TICKET-ID
Type: Story   Priority: Medium
Summary: One line saying what to build.

=== Acceptance Criteria (source: local file) ===
1. First testable criterion - an observable behavior, phrased so a test
   could pass or fail it. ("sub(5, 3) returns 2", not "subtraction works")
2. Second criterion. Each numbered line becomes AC1, AC2, ... and the
   frozen acceptance tests must cover every one of them.
3. Include the error path too - what must happen on bad input.

=== Description ===
What should be built and WHY, in enough detail that a stranger could start.
Name the real files/modules if you know them; say what is out of scope.

Notes for using this template (delete this section in real tickets):
- Copy this file to tickets/<TICKET-ID>.md - the FILENAME is the ticket id.
- The layout above mirrors exactly what a fetched Jira ticket renders to,
  so the spec agent reads it identically.
- The comprehension gate scores this text: missing acceptance criteria or a
  thin description halts the run with questions (printed to the channel and
  evidence/run-report.md - there is no Jira to post them to).
- Keep it pure ASCII.
