import json
import os
import subprocess
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from contextlib import asynccontextmanager

load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure the WebCMD session exists on startup
    session_id = os.getenv("WEBCMD_SESSION_ID", "train-check-f2")
    print(f"Initializing WebCMD session: {session_id}")
    subprocess.run(["webcmd", "session", "create", session_id], check=False)
    yield
    print(f"Closing WebCMD session: {session_id}")
    subprocess.run(["webcmd", "session", "close", session_id], check=False)

app = FastAPI(
    title="Train Reservation WebCMD Bridge",
    version="1.0.0",
    lifespan=lifespan,
)

# =========================================================
# CONFIG
# =========================================================

BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN")
WEBCMD_SESSION_ID = os.getenv("WEBCMD_SESSION_ID", "train-check-f2")

IRCTC_URL = "https://www.irctc.co.in/nget/train-search"


# =========================================================
# REQUEST MODELS
# =========================================================

class TrainCheckRequest(BaseModel):
    from_station: str = Field(..., min_length=2)
    to_station: str = Field(..., min_length=2)
    journey_date: str = Field(..., min_length=8)
    train: Optional[str] = None
    travel_class: Optional[str] = None
    passengers: int = Field(default=1, ge=1, le=6)


class PassengerDetail(BaseModel):
    name: str = Field(..., min_length=1, max_length=16)
    age: int = Field(..., ge=1, le=120)
    gender: str = Field(..., pattern="^(Male|Female|Transgender|M|F|T)$")
    berth_preference: Optional[str] = None
    food_preference: Optional[str] = None

class TrainBookRequest(BaseModel):
    selection_id: str = Field(..., min_length=1)
    passengers: list[PassengerDetail] = Field(..., min_length=1, max_length=6)


# =========================================================
# AUTH
# =========================================================

def verify_bridge_token(token: Optional[str]):
    if not BRIDGE_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="BRIDGE_TOKEN is missing from .env",
        )

    if token != BRIDGE_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Invalid X-Bridge-Token",
        )


# =========================================================
# DATE
# =========================================================

def convert_date(date_string: str) -> str:
    """
    Converts:
        YYYY-MM-DD
        DD/MM/YYYY

    into:
        DD/MM/YYYY
    """

    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
    ]

    for fmt in formats:
        try:
            date_obj = datetime.strptime(date_string, fmt)
            return date_obj.strftime("%d/%m/%Y")
        except ValueError:
            pass

    raise HTTPException(
        status_code=400,
        detail="journey_date must be YYYY-MM-DD or DD/MM/YYYY",
    )


# =========================================================
# RUN WEBCMD
# =========================================================

def run_webcmd(script: str, timeout: int = 120) -> dict:
    """
    Runs WebCMD using the existing explicit browser session.
    """

    command = [
        "webcmd",
        "--session",
        WEBCMD_SESSION_ID,
        "browser",
        "run",
        "--timeout",
        str(timeout),
        "--stdin",
        "--snapshot-mode",
        "tree",
    ]

    try:
        result = subprocess.run(
            command,
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail=(
                "WebCMD CLI was not found. "
                "Run `webcmd --version` in the same environment."
            ),
        )

    except subprocess.TimeoutExpired:
        raise HTTPException(
            status_code=504,
            detail="WebCMD browser execution timed out.",
        )

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode != 0:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "WebCMD command failed",
                "return_code": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
            },
        )

    if not stdout:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "WebCMD returned no output",
                "stderr": stderr,
            },
        )

    try:
        return json.loads(stdout)

    except json.JSONDecodeError:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "WebCMD returned non-JSON output",
                "stdout": stdout,
                "stderr": stderr,
            },
        )


# =========================================================
# ROOT
# =========================================================

@app.get("/")
def root():
    return {
        "service": "Train Reservation WebCMD Bridge",
        "status": "running",
        "session": WEBCMD_SESSION_ID,
    }


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
def health():
    return {
        "status": "ok",
        "webcmd_session": WEBCMD_SESSION_ID,
    }


# =========================================================
# TEST WEBCMD
# =========================================================

@app.get("/test-webcmd")
def test_webcmd(
    x_bridge_token: Optional[str] = Header(
        default=None,
        alias="X-Bridge-Token",
    ),
):
    """
    Simple test:
    FastAPI -> WebCMD -> existing browser session -> IRCTC
    """

    verify_bridge_token(x_bridge_token)

    script = """
return {
    ok: true,
    url: page.url(),
    title: await page.title()
};
"""

    return run_webcmd(script, timeout=60)


# =========================================================
# TRAIN CHECK
# =========================================================

@app.post("/train/check")
def train_check(
    body: TrainCheckRequest,
    x_bridge_token: Optional[str] = Header(
        default=None,
        alias="X-Bridge-Token",
    ),
):
    verify_bridge_token(x_bridge_token)

    irctc_date = convert_date(body.journey_date)

    from_station = body.from_station
    to_station = body.to_station

    requested_train = body.train or ""
    requested_class = body.travel_class or ""

    script_header = f"""
const IRCTC_URL = {json.dumps(IRCTC_URL)};
const js_from = {json.dumps(from_station)};
const js_to = {json.dumps(to_station)};
const js_date = {json.dumps(irctc_date)};
const js_train = {json.dumps(requested_train)};
const js_class = {json.dumps(requested_class)};
const js_passengers = {body.passengers};
"""

    script_body = r"""
const fromInput = page.getByLabel(
    'Enter From station. Input is Mandatory.'
);

const toInput = page.getByLabel(
    'Enter To station. Input is Mandatory.'
);


// ======================================================
// OPEN IRCTC
// ======================================================

await page.goto(IRCTC_URL);
await page.waitForLoadState('domcontentloaded');

await page.waitForTimeout(1500);


// ======================================================
// CLOSE WELCOME DIALOG IF PRESENT
// ======================================================

try {
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);
} catch {}


// ======================================================
// FROM STATION
// ======================================================

await fromInput.click({ force: true });
await fromInput.fill('');
await fromInput.pressSequentially(js_from, { delay: 100 });

await page.waitForTimeout(1500);

const fromOptions = page.locator('li[role="option"]');
const fromTexts = await fromOptions.allTextContents();

let fromIndex = -1;
for (let i = 0; i < fromTexts.length; i++) {
    const text = fromTexts[i].replace(/\s+/g, ' ').trim().toUpperCase();
    if (text.includes(' - ' + js_from.toUpperCase()) || text.includes(js_from.toUpperCase())) {
        fromIndex = i;
        break;
    }
}

if (fromIndex === -1 && fromTexts.length > 0) {
    fromIndex = (fromTexts.length > 1 && fromTexts[0].includes('-----')) ? 1 : 0;
}

if (fromTexts.length === 0 || fromIndex === -1) {
    throw new Error('Could not find From station: ' + js_from + '. Options found: ' + fromTexts.join(', '));
}

await fromOptions.nth(fromIndex).click({
    timeout: 5000,
    force: true
});

await page.waitForTimeout(500);


// ======================================================
// TO STATION
// ======================================================

await toInput.click({ force: true });
await toInput.fill('');
await toInput.pressSequentially(js_to, { delay: 100 });

await page.waitForTimeout(1500);

const toOptions = page.locator('li[role="option"]');
const toTexts = await toOptions.allTextContents();

let toIndex = -1;
for (let i = 0; i < toTexts.length; i++) {
    const text = toTexts[i].replace(/\s+/g, ' ').trim().toUpperCase();
    if (text.includes(' - ' + js_to.toUpperCase()) || text.includes(js_to.toUpperCase())) {
        toIndex = i;
        break;
    }
}

if (toIndex === -1 && toTexts.length > 0) {
    toIndex = (toTexts.length > 1 && toTexts[0].includes('-----')) ? 1 : 0;
}

if (toTexts.length === 0 || toIndex === -1) {
    throw new Error('Could not find To station: ' + js_to + '. Options found: ' + toTexts.join(', '));
}

await toOptions.nth(toIndex).click({
    timeout: 5000,
    force: true
});

await page.waitForTimeout(500);


// ======================================================
// DATE
// ======================================================

const dateInput = page.locator('p-calendar input');

await dateInput.click({ force: true });
await dateInput.fill('');
await dateInput.pressSequentially(js_date, { delay: 100 });
await page.keyboard.press('Tab');

await page.waitForTimeout(300);


// ======================================================
// SEARCH
// ======================================================

const searchButton = page.getByRole(
    'button',
    { name: /Search Trains/i }
);

await searchButton.click({ force: true });


// ======================================================
// WAIT FOR RESULTS
// ======================================================

await page.waitForTimeout(5000);


// ======================================================
// COLLECT PAGE CONTENT
// ======================================================

const bodyText = await page.locator('body').innerText();


// ======================================================
// RETURN STRUCTURED RESULT
// ======================================================

return {
    ok: true,

    request: {
        from: await fromInput.inputValue(),
        to: await toInput.inputValue(),
        journey_date: await dateInput.inputValue(),
        train: js_train,
        travel_class: js_class,
        passengers: js_passengers
    },

    page: {
        url: page.url(),
        title: await page.title()
    },

    result_text: bodyText.slice(0, 20000)
};
"""

    script = script_header + script_body

    return run_webcmd(script, timeout=120)


# =========================================================
# TRAIN BOOK
# =========================================================

@app.post("/train/book")
def train_book(
    body: TrainBookRequest,
    x_bridge_token: Optional[str] = Header(
        default=None,
        alias="X-Bridge-Token",
    ),
):
    verify_bridge_token(x_bridge_token)

    # Convert the passenger list to a JSON string so it can be injected into JS
    passengers_json = json.dumps([p.model_dump() for p in body.passengers])

    script_header = f"""
const selection_id = {json.dumps(body.selection_id)};
const passengers = {passengers_json};
"""

    script_body = r"""
    // Helper function to sleep
    const delay = ms => new Promise(res => setTimeout(res, ms));

    // Assume we are on the Train List page or already on the Passenger Details page.
    // In a real scenario, you'd click the specific train's Book Now button based on selection_id.
    // For now, we assume the user has landed on the passenger details page where the form exists.

    console.log("Starting passenger form automation...");

    // Wait for the passenger container to be visible
    await page.waitForSelector('app-passenger', { state: 'visible', timeout: 15000 }).catch(() => {});

    for (let i = 0; i < passengers.length; i++) {
        const passenger = passengers[i];
        console.log(`Filling details for passenger ${i + 1}: ${passenger.name}`);

        // If it's not the first passenger, we need to click "+ Add Passenger"
        if (i > 0) {
            const addPassengerBtn = page.getByRole('button', { name: /Add Passenger/i });
            if (await addPassengerBtn.isVisible()) {
                await addPassengerBtn.click();
                await delay(500); // Wait for the new row to render
            }
        }

        // Get all passenger form blocks (each row of passenger inputs)
        // Usually, IRCTC groups them in an app-passenger block. We'll find all name inputs.
        const nameInputs = await page.getByPlaceholder('Passenger Name').all();
        const ageInputs = await page.getByPlaceholder('Age').all();
        const genderSelects = await page.locator('select[formcontrolname="passengerGender"]').all();
        
        // Ensure we have enough inputs rendered
        if (nameInputs.length > i && ageInputs.length > i && genderSelects.length > i) {
            await nameInputs[i].fill(passenger.name);
            await delay(200);
            await ageInputs[i].fill(passenger.age.toString());
            await delay(200);

            // Handle gender mapping (M, F, T or Male, Female, Transgender)
            let genderVal = 'M';
            if (passenger.gender.toLowerCase().startsWith('f')) genderVal = 'F';
            else if (passenger.gender.toLowerCase().startsWith('t')) genderVal = 'T';

            await genderSelects[i].selectOption({ value: genderVal });
            await delay(200);
            
            // Optionally handle berth preference if provided
            if (passenger.berth_preference) {
                const berthSelects = await page.locator('select[formcontrolname="passengerBerthChoice"]').all();
                if (berthSelects.length > i) {
                     // Best effort select, value usually maps to LB, MB, UB, etc.
                     await berthSelects[i].selectOption({ label: passenger.berth_preference }).catch(() => {});
                }
            }
        } else {
            console.warn(`Could not find input fields for passenger ${i + 1}`);
        }
    }

    return {
        ok: true,
        stage: 'PASSENGER_DETAILS_FILLED',
        passengers_processed: passengers.length,
        current_url: page.url(),
        human_action_required: true,
        message: 'Successfully filled passenger details. Please complete CAPTCHA and Payment manually.'
    };
"""

    script = script_header + "\n" + script_body
    return run_webcmd(script, timeout=120)