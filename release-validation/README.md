# Release validation — Windows Kodi 21.3

The public-repository fresh install of Custom Play History 0.2.1 was manually verified on Kodi 21.3 for Windows: installing the single setup add-on automatically installed its dependencies, ran guarded SiLVO integration and reloaded the skin; the Music Widget 1 was selected and rendered. The user's later manual review confirmed all four current icons and Jeff Kunsemiller attribution.

In the isolated runtime, the empty-library service started on Schema 13 and remained active. A previous disposable three-track test scanned music into the library and created the initial catalog without a Kodi restart. The empty-library regression suite and setup decision/lifecycle suite each passed eight tests.

On 2026-11-25 at 18:31:04, Kodi successfully read and committed the public repository catalog, checked the official repository, and automatically updated service 0.1.4 to 0.1.5, integration 0.1.4 to 0.1.5, and setup 0.2.3 to 0.2.4. The setup verified the existing integration without rewriting skin files. All current versions advertise Jeff Kunsemiller.

An earlier repository update remained at 0%; its exact cause was not established. One later retry was invalidated by restricted test-process networking; the subsequent normal-network retry succeeded without a repository modification. The successful update was not a comprehensive historical-version upgrade or listening-history preservation test.

Android and Fire OS simplified-install runtime behavior, comprehensive repository-based historical upgrades, and prerequisite-recovery after an unsupported initial installation remain unverified. Previous individual components have operated on the project owner's Shield and a friend's Fire Stick.
