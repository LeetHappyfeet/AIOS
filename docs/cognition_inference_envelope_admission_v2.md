# Historical cognition inference: bounded response adapter and admission

## Boundary

The provider's raw `response_text` is retained unchanged. Only requests
declaring `x-aios-envelope: cognition-units-v1`, `type: object`,
`properties.units.type: array`, and no action authority may adapt a bare
JSON array of no more than four objects into `{"units":[...]}`. Normal
object replies pass through. Other inference callers still require an object
root, and scalar, mixed/nested, oversized and invalid JSON are rejected.

The normalized `response_json` and returned raw inference payload carry
`_aios_response_envelope` (either `object` or
`bare_units_array_normalized`). The original raw response is available
in `response_text` for diagnosis.

## Source admission

The bounded inference worker now records
`enrichment_admission_version=bounded-enrichment-admission-v2-source` and
specific `enrichment_rejections`. Candidate admission deliberately rejects
an unsupported multi-obligation expansion of an elliptical agreement, positive
GOAL inference from an avoidance/refusal, immediate resource requests
presented as managed plans, and goals whose proposed objectives cannot be
located in the owning excerpt. A prior speaker's utterance remains context,
not unilateral adoption authority. Existing deterministic author identity,
confidence, valid objective, temporal safeguards and chronological
reconciliation still apply. A rejected candidate is not rewritten into
an alternative cognition unit by the model.

On failure to complete historical enrichment, the catch-up status now
reports the latest persisted `message_cognition` inference attempt
(status/error/validation_error) rather than labelling every no-progress case
as provider unavailability.

## Recovery

Restart relevant AIOS workers after pulling the development branch. With a
registered/routable structured-output provider, re-run the bounded recovery:

```bash
python -m aios_app.epistemic.cognition_catchup \
  --instance-id INSTANCE_UUID --max-batches 8
```

Review the enriched commit summary and active managed goals. A successful
enrichment can admit zero goals when source evidence does not justify them.
Do not delete or directly clear deferred receipts; this pass preserves
chronological adjudication and historical V9/V10 provenance.

The scoped fast CI tests use Character_A/Speaker_B fixtures and include
provider-envelope regressions shaped like the two failed requests.
