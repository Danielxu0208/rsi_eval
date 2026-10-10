# Third-party code

`src/rsi_eval/harbor_codex.py` adapts the run lifecycle from the Codex agent in
[Harbor v0.18.0](https://github.com/harbor-framework/harbor/tree/v0.18.0),
licensed under Apache-2.0. The changes load candidate artifacts, serialize
subscription authentication, persist refreshed credentials and retain unknown
subscription dollar cost. The Apache-2.0 license text is included under
`LICENSES/Apache-2.0.txt`.

The method integrations execute separately obtained upstream checkouts:

- [Meta-Harness](https://github.com/stanford-iris-lab/meta-harness), MIT,
  copyright 2026 Yoonho Lee, commit `8123ccabe2b19fa1123090b2e5f1bacc20963ce8`.
- [RRSI](https://github.com/google-research/rrsi), Apache-2.0,
  commit `be50316e1db05914068a973f322770ef08ed7ba1`.
- [Agentic Harness Engineering](https://github.com/china-qijizhifeng/agentic-harness-engineering),
  MIT, commit `8b2a55d97590363fe50c3cc6b5e833b020a4bb4c`.
  Its authors describe the bundled Agent Debugger as partially open source.
- [HarnessX](https://github.com/Darwin-Agent/HarnessX), MIT,
  commit `915520584c92cc35ed24aef1af17eec1d60c0b01`.
  The adapter targets the published harness-evolution implementation.

Managed checkouts retain the upstream license files. The adapters document
their changes and do not claim to reproduce the upstream paper scores.
Pinned source fixtures under `tests/fixtures/ahe_native/` and
`tests/fixtures/harnessx_native/` retain their
upstream notices. They support offline checks of the original control flow;
fixture responses do not constitute live method validation.

The MGSM adapter downloads the upstream TSVs and their CC BY 4.0 license from
[google-research/url-nlp](https://github.com/google-research/url-nlp/tree/452a21ad3dae5668c06ceeac21ff073e1e40f9be/mgsm).
It records the revision and checksums in the catalog and preserves the license
with each prepared run. The dataset is not redistributed in the Python wheel.

The LiveCodeBench adapter downloads the unmodified MIT-licensed testing utility
at commit `28fef95ea8c9f7a547c8329f2cd3d32b92c1fa24`. A matching copy and its
license appear under `tests/fixtures/livecodebench/` for offline verification.
The task generator preserves the license alongside each prepared verifier.
Dataset files remain external, identified by immutable revision and SHA256.

EngDesign fixtures under `tests/fixtures/engdesign/` retain the upstream MIT
license. The materializer downloads task files from commit
`c8ec338cc5f846d792d93728b00c9448111139e5`, checks each hash, and preserves the
license with every task. It runs the original schema and grader without edits.
The integration reference solutions are declared separately from upstream code.

The complete EngDesign adapter also downloads 14 Webots R2025a background
PROTO/texture assets from Cyberbotics' commit
`c6793d8f7230a311c4bc2a3101d9f1a8bc0aa01b` at runtime. These assets retain the
[Webots assets license](https://cyberbotics.com/webots_assets_license) and are
not redistributed in this repository or its Python distributions. Two world
file URL substitutions point to the same local assets; the adapter records
both original and patched hashes. EngDesign schemas and evaluators remain
unchanged.

DROP uses the released Allen Institute dataset under CC BY-SA 4.0. Its zip file
and extracted split files remain external and checksummed. Small dataset fixtures
under `tests/fixtures/drop/` preserve the data license. The unchanged scorer comes
from [allennlp-models](https://github.com/allenai/allennlp-models) at commit
`b1f372248c17ad12684d344955fbcd98e957e77e` under Apache-2.0. Its fixture and runtime
copies retain the upstream license.

Frontier-Eng task code remains an external runtime download from
[Einsia/Frontier-Engineering](https://github.com/Einsia/Frontier-Engineering) at
commit `bafa7d53162a3b1f128fec396bbde22fe972bf24`. The adapter checks every listed
source hash and preserves native scoring code. This checkout has no root license
file; the package does not redistribute its task sources or assign them a new
license. Runtime dependencies retain their own upstream licenses.

The reasoning adapters download pinned public sources from
[MMLU](https://github.com/hendrycks/test),
[GPQA](https://github.com/idavidrein/gpqa), and
[ARC-AGI](https://github.com/fchollet/ARC-AGI). GPQA includes its CC BY 4.0 license
in the original archive. These datasets remain external runtime downloads.
The three unmodified MCE source files under `tests/fixtures/mce_native/` come
from the Meta-Harness commit above and retain its MIT license in that directory.
The MCE data releases retain their original dataset terms; an MCE configuration
does not represent the complete underlying FiNER, USPTO, LawBench or AEGIS corpus.

JobBench downloads the Apache-2.0 evaluator at
`660b33a86ed032559df0b2fdc1ad38cf7ceb83f3` and the dataset at
`d43a66575635da0d3f6cb9a20976dc1a60f3ffc9` (its pinned data card declares MIT).
Offline fixtures under `tests/fixtures/jobbench/` include the original evaluator
archive and two public task fixtures with their licenses. Native judge and
document-extraction code are unchanged.

Harvey LAB task code and documents remain external downloads from
`harveyai/harvey-labs` at `a15650fe74bb20b86f160aaabdad75b87a91b6e8`
(MIT, copyright 2026 Harvey AI).
The adapter preserves native rubric grading and records source hashes. Its
dataset and runtime dependencies remain subject to the publisher's terms.

APEX-Agents 1.1.1 task packages and container images remain external, immutable
Harbor downloads. The adapter uses Harbor's publicly distributed application
key for anonymous public reads; it is not a user credential. APEX data is
published for evaluation only, not training, fine-tuning or parameter fitting.
The package does not redistribute APEX prompts, gold answers or task documents.

GDPval and TTHE have been removed from the active package. Historical validation
reports retain their original names, source revisions and attribution; their
adapters and fixtures are no longer distributed.
