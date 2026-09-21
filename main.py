import os, io, re, json, base64, hashlib, logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field
from PIL import Image

APP_VERSION = "2.3-chatgpt-creative-engine-creation-first"
BASE_DIR = Path(__file__).resolve().parent
ASSETS = BASE_DIR / "assets"
REF = ASSETS / "staline_reference.png"
LOCKUP = ASSETS / "staline_brand_lockup.png"
LAST_LOOKBOOK_REF = ASSETS / "staline_last_lookbook_reference.png"

PRINTIFY_TOKEN = os.getenv("PRINTIFY_API_TOKEN", "").strip()
PRINTIFY_SHOP_ID = os.getenv("PRINTIFY_SHOP_ID", "").strip()
PRINTIFY_BASE = os.getenv("PRINTIFY_BASE", "https://api.printify.com/v1").rstrip("/")
PRINTIFY_AUTO_PUBLISH = os.getenv("PRINTIFY_AUTO_PUBLISH", "false").lower() == "true"
MAX_VARIANTS = max(1, int(os.getenv("STALINE_MAX_VARIANTS", "24")))
CATALOG_LIMIT = max(10, min(int(os.getenv("STALINE_CATALOG_LIMIT", "100")), 100))
MARKUP = max(1.0, float(os.getenv("STALINE_MARKUP", "2.35")))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "90"))

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("staline")
app = FastAPI(title="STALINE — ChatGPT Creative Bridge", version=APP_VERSION)

# -----------------------------------------------------------------------------
# STALINE CREATIVE CONTRACT
# The creative work is intentionally owned by ChatGPT in the conversation.
# This bridge does NOT call Gemini/OpenAI to invent a design. It receives the
# approved design + production files and translates them to the POD provider.
# -----------------------------------------------------------------------------
CREATION_FIRST_RULES = {
    "principle": "ChatGPT creates the garment first; production catalog selection comes second.",
    "creative_engine": "ChatGPT",
    "reference_hierarchy": ["staline_last_lookbook_reference.png", "staline_reference.png", "staline_brand_lockup.png"],
    "separate_editorial_and_production_artwork": True,
    "user_approval_required_before_production": True,
}
CREATIVE_DNA = """
STALINE is a premium fashion identity, not a logo placed on generic merchandise.
The garment must remain recognisable as STALINE even if every word is removed.

CORE FUSION:
- Congolese SAPE: tailoring, elegance, deliberate styling, confident colour dialogue,
  refined proportion and presence.
- Cameroon / Grassfields / Toghu-inspired visual grammar: geometric rhythm,
  structured bands, diamonds, stepped geometry, embroidery-like density and borders.
  Do not copy sacred/cultural garments literally and do not make costume replicas.
- Italian / Neapolitan fashion: relaxed tailoring, asymmetry, fluid drape, precise
  finishing, quiet luxury and sophisticated construction.
- Contemporary technical fashion: engineered panels, modular seams, utility details,
  technical textile logic and controlled construction.
- STALINE Market Intelligence: candlestick rhythm, price structure, liquidity,
  order-flow, market grids and microstructure translated into garment construction,
  textile geometry and engineered motifs — never a generic trading poster.

No three-colour limitation. Use any coherent premium palette.
No generic cyberpunk, gaming jersey, random blue-vector trading graphics, flags,
cartoon stereotypes, tourist graphics or empty poster layouts.
The objective is a real fashion collection with a new STALINE visual language.
"""

POSITIVE = [
    "hoodie", "sweatshirt", "tee", "t-shirt", "shirt", "long sleeve", "jacket",
    "coat", "track", "oversized", "heavyweight", "premium", "pants", "trousers",
    "shorts", "dress", "skirt", "tank", "vest", "all-over", "aop", "cut & sew",
    "embroidery", "dtg", "dtf", "zip", "pullover", "windbreaker", "bomber"
]
NEGATIVE = [
    "mug", "poster", "phone", "sticker", "mouse", "puzzle", "canvas", "notebook",
    "keychain", "ornament", "flag", "pillow", "blanket", "wall art", "phone case"
]

class PlacementSpec(BaseModel):
    file: str
    x: float = Field(default=0.5, ge=0.0, le=1.0)
    y: float = Field(default=0.5, ge=0.0, le=1.0)
    scale: float = Field(default=1.0, gt=0.0, le=5.0)
    angle: float = Field(default=0.0, ge=-360.0, le=360.0)
    notes: str = ""

class DesignSpec(BaseModel):
    name: str
    collection: str = "STALINE"
    gender: str = "unisex"
    product_type: str = ""
    product_family: str = ""
    editorial_concept: str = ""
    silhouette: str = ""
    fit: str = ""
    construction: List[str] = Field(default_factory=list)
    materials_visual: List[str] = Field(default_factory=list)
    palette: List[str] = Field(default_factory=list)
    primary_motifs: List[str] = Field(default_factory=list)
    secondary_motifs: List[str] = Field(default_factory=list)
    cultural_fusion: Dict[str, str] = Field(default_factory=dict)
    market_translation: List[str] = Field(default_factory=list)
    placements: Dict[str, PlacementSpec] = Field(default_factory=dict)
    decoration_methods: List[str] = Field(default_factory=list)
    logo_usage: str = "Official STALINE wordmark used elegantly and secondarily; never the whole design."
    typography: str = "Secondary to the garment design."
    colorways: List[str] = Field(default_factory=list)
    model_direction: str = ""
    styling_direction: str = ""
    photography_direction: str = ""
    product_description: str = ""
    fabrication_notes: str = ""
    publish: bool = False

class PrepareRequest(BaseModel):
    gender: str = "unisex"
    product_hint: str = ""
    required_positions: List[str] = Field(default_factory=lambda: ["front"])
    required_decoration: str = "auto"
    prefer_aop_cut_sew: bool = True

class ValidateRequest(BaseModel):
    design: DesignSpec


def require_printify() -> None:
    missing = []
    if not PRINTIFY_TOKEN:
        missing.append("PRINTIFY_API_TOKEN")
    if not PRINTIFY_SHOP_ID:
        missing.append("PRINTIFY_SHOP_ID")
    if missing:
        raise HTTPException(500, "Variables manquantes: " + ", ".join(missing))


def p_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {PRINTIFY_TOKEN}", "Content-Type": "application/json"}

async def pget(path: str, params: Optional[dict] = None) -> Any:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.get(f"{PRINTIFY_BASE}{path}", headers=p_headers(), params=params)
    if response.status_code >= 400:
        raise HTTPException(response.status_code, f"Printify GET {path}: {response.text[:1600]}")
    return response.json()

async def ppost(path: str, payload: dict) -> Any:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(f"{PRINTIFY_BASE}{path}", headers=p_headers(), json=payload)
    if response.status_code >= 400:
        raise HTTPException(response.status_code, f"Printify POST {path}: {response.text[:2000]}")
    return response.json()


def safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", value)[:100]


def normalize_upload(raw: bytes, filename: str) -> Path:
    outdir = BASE_DIR / "generated"
    outdir.mkdir(exist_ok=True)
    try:
        image = Image.open(io.BytesIO(raw))
        ext = Path(filename).suffix.lower()
        if ext not in {".png", ".jpg", ".jpeg"}:
            ext = ".png"
        image = image.convert("RGBA" if ext == ".png" else "RGB")
        if max(image.size) > 7000:
            factor = 7000 / max(image.size)
            image = image.resize((max(1, int(image.width * factor)), max(1, int(image.height * factor))), Image.Resampling.LANCZOS)
        digest = hashlib.sha1(raw).hexdigest()[:12]
        path = outdir / f"{safe_name(Path(filename).stem)}_{digest}{ext}"
        image.save(path, "PNG" if ext == ".png" else "JPEG", optimize=True)
        return path
    except Exception as exc:
        raise HTTPException(400, f"Image invalide ({filename}): {exc}")


def unwrap_list(data: Any, keys: tuple = ("data", "variants", "items")) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return data[key]
    return []

async def catalog_blueprints() -> list:
    data = await pget("/catalog/blueprints.json", {"limit": CATALOG_LIMIT})
    return unwrap_list(data)


def score_blueprint(bp: dict, gender: str, hint: str = "") -> float:
    text = (str(bp.get("title", "")) + " " + str(bp.get("description", ""))).lower()
    query = (hint or "").lower()
    score = 0.0
    for word in POSITIVE:
        if word in text:
            score += 2.0
    for word in NEGATIVE:
        if word in text:
            score -= 10.0
    if any(word in text for word in ("all-over", "aop", "cut & sew")):
        score += 12.0
    if gender == "women" and any(word in text for word in ("women", "dress", "skirt")):
        score += 5.0
    if gender == "men" and any(word in text for word in ("men", "jacket", "shirt", "oversized")):
        score += 4.0
    for token in re.findall(r"[a-z0-9-]{3,}", query):
        if token in text:
            score += 1.5
    return score


def placeholder_name(ph: dict) -> str:
    return str(ph.get("position") or ph.get("name") or "front")


def extract_placeholders(variants: list) -> List[dict]:
    result = []
    seen = set()
    for variant in variants:
        for ph in variant.get("placeholders", []) or []:
            if not isinstance(ph, dict):
                continue
            key = json.dumps(ph, sort_keys=True, default=str)
            if key not in seen:
                result.append(ph)
                seen.add(key)
    return result

async def choose_product(req: PrepareRequest | ValidateRequest | DesignSpec):
    if isinstance(req, DesignSpec):
        gender = req.gender
        hint = f"{req.product_type} {req.product_family} {req.silhouette}"
        required_positions = list(req.placements.keys())
        required_decoration = ",".join(req.decoration_methods) if req.decoration_methods else "auto"
        prefer_aop = True
    else:
        gender = req.gender
        hint = req.product_hint if hasattr(req, "product_hint") else ""
        required_positions = req.required_positions if hasattr(req, "required_positions") else ["front"]
        required_decoration = req.required_decoration if hasattr(req, "required_decoration") else "auto"
        prefer_aop = req.prefer_aop_cut_sew if hasattr(req, "prefer_aop_cut_sew") else True

    blueprints = await catalog_blueprints()
    ranked = sorted(blueprints, key=lambda b: score_blueprint(b, gender.lower(), hint), reverse=True)
    candidates = []

    for bp in ranked[:25]:
        bid = bp.get("id")
        if not bid:
            continue
        try:
            provider_data = await pget(f"/catalog/blueprints/{bid}/print_providers.json")
            providers = unwrap_list(provider_data)
        except Exception as exc:
            log.warning("providers %s: %s", bid, exc)
            continue

        for provider in providers[:10]:
            pid = provider.get("id")
            if not pid:
                continue
            try:
                variant_data = await pget(f"/catalog/blueprints/{bid}/print_providers/{pid}/variants.json")
                variants = unwrap_list(variant_data)
            except Exception as exc:
                log.warning("variants %s/%s: %s", bid, pid, exc)
                continue

            placeholders = extract_placeholders(variants)
            positions = {placeholder_name(p).lower() for p in placeholders}
            methods = {str(p.get("decoration_method", "")).lower() for p in placeholders}
            title = str(bp.get("title", ""))
            text = title.lower()
            score = score_blueprint(bp, gender.lower(), hint)
            if prefer_aop and any(x in text for x in ("all-over", "aop", "cut & sew")):
                score += 12
            if len(positions) > 1:
                score += 5
            if required_positions and all(p.lower() in positions for p in required_positions):
                score += 12
            elif required_positions:
                score -= 18
            if required_decoration != "auto":
                requested = {x.strip().lower() for x in required_decoration.split(",") if x.strip()}
                if requested & methods:
                    score += 10
                else:
                    score -= 6
            if "embroidery" in methods:
                score += 2
            candidates.append((score, bp, provider, variants, placeholders, methods, positions))

    if not candidates:
        raise HTTPException(502, "Aucun produit textile exploitable trouvé dans le catalogue Printify.")

    candidates.sort(key=lambda x: x[0], reverse=True)
    score, bp, provider, variants, placeholders, methods, positions = candidates[0]
    return {
        "score": round(score, 2),
        "blueprint": bp,
        "provider": provider,
        "variants": variants,
        "placeholders": placeholders,
        "methods": sorted(methods),
        "positions": sorted(positions),
    }


def enabled_variants(product: dict) -> List[dict]:
    variants = [v for v in product["variants"] if v.get("is_enabled", True)]
    if len(variants) <= MAX_VARIANTS:
        return variants
    defaults = [v for v in variants if v.get("is_default")]
    selected = defaults[:]
    for variant in variants:
        if len(selected) >= MAX_VARIANTS:
            break
        if variant not in selected:
            selected.append(variant)
    return selected[:MAX_VARIANTS]


def find_placeholder(product: dict, position: str) -> Optional[dict]:
    wanted = position.lower()
    for placeholder in product["placeholders"]:
        if placeholder_name(placeholder).lower() == wanted:
            return placeholder
    return None


def validate_design_against_product(design: DesignSpec, product: dict) -> None:
    if not design.placements:
        raise HTTPException(400, "Le design doit contenir au moins une zone de placement.")
    available = {p.lower() for p in product["positions"]}
    missing = sorted({p.lower() for p in design.placements} - available)
    if missing:
        raise HTTPException(400, f"Zones non disponibles chez le fournisseur: {missing}. Le bridge refuse de déformer le design.")
    if not design.palette:
        raise HTTPException(400, "La fiche créative doit contenir une palette.")
    if not design.silhouette:
        raise HTTPException(400, "La fiche créative doit préciser la silhouette.")
    if not design.construction:
        raise HTTPException(400, "La fiche créative doit préciser la construction.")

async def upload_printify_image(path: Path, filename: Optional[str] = None) -> dict:
    contents = base64.b64encode(path.read_bytes()).decode()
    return await ppost("/uploads/images.json", {"file_name": filename or path.name, "contents": contents})

async def create_product(design: DesignSpec, artworks: List[UploadFile], publish: bool) -> dict:
    product = await choose_product(design)
    validate_design_against_product(design, product)
    if not artworks:
        raise HTTPException(400, "Au moins un artwork approuvé par ChatGPT est requis.")

    uploaded: Dict[str, dict] = {}
    for upload in artworks:
        raw = await upload.read()
        if not raw:
            continue
        original = upload.filename or "artwork.png"
        path = normalize_upload(raw, original)
        uploaded[original] = await upload_printify_image(path, original)

    variants = enabled_variants(product)
    if not variants:
        raise HTTPException(502, "Aucune variante activée chez ce fournisseur.")

    print_areas = []
    for position, settings in design.placements.items():
        filename = settings.file
        if filename not in uploaded:
            raise HTTPException(400, f"Artwork '{filename}' manquant pour la zone '{position}'.")
        placeholder = find_placeholder(product, position)
        if not placeholder:
            raise HTTPException(400, f"Zone '{position}' indisponible chez le fournisseur.")
        print_areas.append({
            "position": placeholder_name(placeholder),
            "images": [{
                "id": uploaded[filename]["id"],
                "x": settings.x,
                "y": settings.y,
                "scale": settings.scale,
                "angle": settings.angle,
            }],
        })

    out_variants = []
    for variant in variants:
        cost = int(variant.get("price", 0))
        out_variants.append({
            "id": variant["id"],
            "price": max(cost + 1, int(round(cost * MARKUP))),
            "is_enabled": True,
        })

    payload = {
        "title": f"STALINE {design.name}",
        "description": design.product_description or f"{design.name} — création originale STALINE.",
        "blueprint_id": product["blueprint"]["id"],
        "print_provider_id": product["provider"]["id"],
        "variants": out_variants,
        "print_areas": print_areas,
        "tags": ["STALINE", "premium fashion", "adaptive fashion", "technical fashion", "market intelligence"],
    }
    created = await ppost(f"/shops/{PRINTIFY_SHOP_ID}/products.json", payload)
    result = {
        "creative_engine": "ChatGPT",
        "generation_engine": None,
        "important": "Les images, la direction artistique et la fiche créative ont été produites/validées dans ChatGPT. Ce bridge ne génère pas le design.",
        "product": created,
        "design": design.model_dump(),
        "uploaded_media": uploaded,
        "selected_catalog": {
            "score": product["score"],
            "blueprint": product["blueprint"],
            "provider": product["provider"],
            "positions": product["positions"],
            "methods": product["methods"],
            "variant_count": len(variants),
        },
        "published": False,
    }
    if publish:
        result["publish"] = await ppost(f"/shops/{PRINTIFY_SHOP_ID}/products/{created['id']}/publish.json", {
            "title": True,
            "description": True,
            "images": True,
            "variants": True,
            "tags": True,
            "keyFeatures": True,
        })
        result["published"] = True
    return result

@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": APP_VERSION,
        "creative_engine": "ChatGPT",
        "this_service_generates_art": False,
        "printify_shop_configured": bool(PRINTIFY_SHOP_ID),
        "reference": REF.exists(),
        "brand_lockup": LOCKUP.exists(),
    }

@app.get("/api/creative-contract")
async def creative_contract():
    return {
        "creative_engine": "ChatGPT",
        "creative_dna": CREATIVE_DNA,
        "reference_assets": {"style_reference": REF.exists(), "brand_lockup": LOCKUP.exists()},
        "workflow": [
            "ChatGPT creates and validates the garment concept.",
            "ChatGPT produces the artwork and optional editorial/model images.",
            "The user approves the design.",
            "This bridge selects a compatible real Printify product/provider.",
            "The bridge uploads production artwork and exact placement data.",
            "Printify creates the product and can publish it when explicitly enabled.",
        ],
    }

@app.post("/admin/staline/validate-design")
async def validate_design(req: ValidateRequest):
    require_printify()
    product = await choose_product(req.design)
    validate_design_against_product(req.design, product)
    return {
        "valid": True,
        "ready_for_production": True,
        "creative_engine": "ChatGPT",
        "selected_catalog": {
            "score": product["score"],
            "blueprint": product["blueprint"],
            "provider": product["provider"],
            "positions": product["positions"],
            "methods": product["methods"],
        },
    }

@app.post("/admin/staline/prepare")
async def prepare(req: PrepareRequest):
    require_printify()
    product = await choose_product(req)
    return {
        "ready_for_chatgpt_design": True,
        "creative_engine": "ChatGPT",
        "creative_dna": CREATIVE_DNA,
        "selected_catalog": {
            "score": product["score"],
            "blueprint": product["blueprint"],
            "provider": product["provider"],
            "positions": product["positions"],
            "decoration_methods": product["methods"],
            "placeholders": product["placeholders"],
        },
        "instruction": "Créer maintenant le vêtement et les artworks dans ChatGPT. Ce service ne génère aucune image.",
    }

@app.get("/api/creative-brief")
async def creative_brief():
    return {
        "version": APP_VERSION,
        "architecture": "creation-first",
        "creative_engine": "ChatGPT",
        "instruction": "Create a genuinely new STALINE garment before selecting a production blueprint.",
        "reference_hierarchy": [
            {"file": LOOKBOOK.name, "role": "current visual benchmark"},
            {"file": REF.name, "role": "original STALINE visual/brand reference"},
            {"file": LOCKUP.name, "role": "official STALINE identity asset"},
        ],
        "required_creation_layers": [
            "creative thesis", "silhouette and fit", "garment architecture",
            "panel/seam map", "materials and trims", "unrestricted coherent palette",
            "cultural fusion visible in construction", "STALINE market-intelligence translation",
            "men/women model and styling direction", "editorial shot list",
            "separate production artworks", "manufacturing constraints", "commerce metadata"
        ],
        "forbidden_shortcut": "Do not start from a generic POD product and merely paste a STALINE logo.",
    }

@app.get("/api/printify/catalog")
async def catalog(gender: str = "unisex", product_hint: str = ""):
    require_printify()
    blueprints = await catalog_blueprints()
    ranked = sorted(blueprints, key=lambda x: score_blueprint(x, gender.lower(), product_hint), reverse=True)
    return [{"id": b.get("id"), "title": b.get("title"), "score": score_blueprint(b, gender.lower(), product_hint)} for b in ranked[:30]]

@app.post("/admin/staline/create")
async def create(
    design_json: str = Form(...),
    artworks: List[UploadFile] = File(...),
    publish: Optional[bool] = Form(None),
):
    require_printify()
    try:
        design = DesignSpec.model_validate_json(design_json)
    except Exception as exc:
        raise HTTPException(400, f"design_json invalide: {exc}")
    do_publish = PRINTIFY_AUTO_PUBLISH if publish is None else publish
    return await create_product(design, artworks, do_publish)
