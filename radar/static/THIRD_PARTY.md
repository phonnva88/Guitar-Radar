# Frontend dependencies

These browser assets are checked into `vendor/` and served by the authenticated
application under `/static/vendor/`. The application does not contact a CDN,
load remote scripts or fonts, or require Node.js to run.

| Package | Pinned version | License | Distributed assets used | Source |
| --- | --- | --- | --- | --- |
| Bootstrap | 5.3.8 | MIT | `bootstrap/bootstrap.min.css` | [Official npm tarball](https://registry.npmjs.org/bootstrap/-/bootstrap-5.3.8.tgz) |
| Tabulator Tables | 6.3.1 | MIT | `tabulator/tabulator.min.js`, `tabulator/tabulator.min.css` | [Official npm tarball](https://registry.npmjs.org/tabulator-tables/-/tabulator-tables-6.3.1.tgz) |
| PhotoSwipe | 5.4.4 | MIT | `photoswipe/photoswipe.esm.min.js`, `photoswipe/photoswipe-lightbox.esm.min.js`, `photoswipe/photoswipe.css` | [Official npm tarball](https://registry.npmjs.org/photoswipe/-/photoswipe-5.4.4.tgz) |
| Lucide | 1.70.0 | ISC and upstream icon notices | `lucide/lucide.min.js` | [Official npm tarball](https://registry.npmjs.org/lucide/-/lucide-1.70.0.tgz) |

Each package's full license and required notices are preserved in its `LICENSE`
file. Bootstrap JavaScript and its Popper dependency are not included or needed.
Tabulator and Lucide expose the browser globals `Tabulator` and `lucide`.
PhotoSwipe and PhotoSwipe Lightbox use local ES-module default exports.

`vendor/manifest.json` records the registry tarball URL, SHA512 integrity value,
SHA256 of the downloaded tarball, and SHA256 of every checked-in asset/license.
Tarball SHA512 digests were checked against the official registry metadata
before extracting the assets. Only the listed distributed files and licenses
were copied. The files are unchanged except that `sourceMappingURL` comments
were removed so browsers do not request omitted development maps.
The repository preserves vendor file bytes without Git line-ending conversion
so these checksums remain valid after a Windows or Linux checkout.

For an upgrade, fetch the exact new version from its official npm registry
metadata, verify its tarball integrity, preserve its license, update the manifest
and this inventory, and verify the audit table, galleries, icons, mobile layout,
and existing application flows under the application's Content Security Policy.
