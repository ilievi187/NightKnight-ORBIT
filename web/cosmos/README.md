# COSMOS visuals — ALEX / NOVA / HERMES

This directory is the destination for the six locally downloaded NightKnight COSMOS assets. The images are intentionally **not committed** to source control. Frontend code under web/cosmos.js uses generated planet textures and CSS nebula gradients if they have not been installed.

From the root of the checkout:

```bash
python scripts/install_cosmos_assets.py
# Or specify the download directory/ZIP:
python scripts/install_cosmos_assets.py ~/Downloads
python scripts/install_cosmos_assets.py ~/Downloads/NightKnight-COSMOS-ALEX-NOVA-assets.zip
```

Files expected, with their exact names:
- cosmos_alex_background.png (3840×2160)
- cosmos_nova_background.png (3840×2160)
- planet_violet_texture.png (4096×2048)
- planet_blue_texture.png (4096×2048)
- planet_amber_texture.png (4096×2048)
- planet_moon_texture.png (4096×2048)

The installer verifies each file's dimensions and original SHA-256 before copying. Installed assets are served only by the existing loopback FastAPI server at /assets/cosmos/. The WebGL renderer downsamples 4096×2048 source textures to 1024×512 before GPU upload. No external JavaScript packages, tracking or CDN access are required.

The 3D preview is **visualization only**. Profile buttons create ORBIT sessions as before; they do not imply ALEX/NOVA/HERMES are live remote processes. If WebGL fails, a static CSS fallback remains. Users with reduced-motion preferences see a still scene.

If the scene shows the procedurally generated texture after installation, force refresh the browser (Ctrl+Shift+R). Do not expose ORBIT to the public internet; it does not yet have user authentication.
