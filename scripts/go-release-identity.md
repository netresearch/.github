# Go release source identity

Both Go release reusables require the selected release tag, triggering tag ref,
event commit and source checkout to agree. This also applies to manual runs:
dispatch the workflow at the intended tag, and pass the same tag or leave the
override empty. Dispatching `main` with a tag override is rejected, including
when both refs happen to point at the same commit. Two tags at the same commit
are different release identities.

Artifact attestations derive source ref and commit from the workflow's OIDC
claims. Checking out another ref does not change those claims. Consequently a
checkout-only fix for manual backfills would still record misleading source
identity in provenance.

The library keeps its two-job graph and race tests. Both jobs check out immutable
commits. Application binary/container jobs check out the immutable commit
resolved by preflight. The validated tag object is checked again before each
image or GitHub release publisher, including the final draft-to-published step;
read failures and moved, replaced or deleted tags fail closed. An external tag
writer can still race the final read and publication; tag protection remains
necessary because these operations cannot form one cross-service transaction.

Existing SBOMs, checksums, platforms, Cosign signatures, attestations, annotated
tag requirements and signature-trust policy are retained. No actual release is
needed to test this contract: offline fixtures execute the source guards using
temporary local commits/tags and a stub that cannot contact GitHub or publish.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s scripts -p 'test_go_release_identity.py' -v
```
