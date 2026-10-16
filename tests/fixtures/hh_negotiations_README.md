B3-10 fixtures were reduced offline from saved vacancy pages dated 2026-10-06:

- `hh_negotiations_two.html`: `hh_137870570_081228/vacancy_open-duunicj4/vacancy_open.html`; two topics with different resume IDs.
- `hh_negotiations_zero.html`: `hh_138151000_080921/vacancy_open-w7v1o2e1/vacancy_open.html`; zero topics.

Only the negotiations structure and resume ID-to-hash structure remain. Hashes are synthetic. Names, contacts, resume details, letters, tokens, cookies, other state and active page markup were removed before writing these fixtures.

The original trace sanitizer damaged unrelated JSON in the first page's template. Fixture reduction decoded the intact `applicantVacancyResponseStatuses` and `resumes` objects separately. The production parser intentionally rejects malformed JSON rather than attempting this recovery.
