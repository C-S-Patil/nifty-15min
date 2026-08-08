import datetime
import pandas as pd


def get_active_nifty_option_symbol(
    kite_client,
    spot_price: float,
    option_type: str,
    strike_offset: int = 0,
) -> str:
    instruments = kite_client.instruments("NFO")
    df = pd.DataFrame(instruments)

    nifty_options = df[
        (df["name"] == "NIFTY") & (df["segment"] == "NFO-OPT")
    ].copy()

    today = datetime.date.today()
    nifty_options["expiry"] = pd.to_datetime(nifty_options["expiry"]).dt.date
    valid_options = nifty_options[nifty_options["expiry"] >= today]

    nearest_expiry = valid_options["expiry"].min()
    atm_strike = round(spot_price / 50) * 50
    target_strike = atm_strike + strike_offset

    target_contract = valid_options[
        (valid_options["expiry"] == nearest_expiry)
        & (valid_options["strike"] == target_strike)
        & (valid_options["instrument_type"] == option_type)
    ]

    if target_contract.empty:
        raise ValueError(f"No contract found for Strike: {target_strike}")

    return target_contract.iloc[0]["tradingsymbol"]