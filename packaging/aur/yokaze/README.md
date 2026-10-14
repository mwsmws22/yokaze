# AUR package: yokaze

PKGBUILD for the **public** Yokaze release (user identity, logo icon).

Source tarball comes from `mwsmws22/yokaze` release tags.

## Publish flow

1. Tag a release on the public mirror (`just publish-public` / `/publish-public`).
2. Update `pkgver` / checksums in this PKGBUILD.
3. Submit or update the AUR package **when AUR registration is open** — see
   [`../PUBLISH-TODO.md`](../PUBLISH-TODO.md).

Workbench installs use `just install` (Dev identity), not this package.
