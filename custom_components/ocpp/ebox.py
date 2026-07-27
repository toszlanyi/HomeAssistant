"""
eBox Smart - OCPP 1.6 charge point override.
Version v2.1.1

Activation - change THIS import line in upstream ocpp api.py
------------------------------------------------------------
before:
    from .ocppv16 import ChargePoint as ChargePointv16
after:
    from .ebox import ChargePoint as ChargePointv16

Strategy - Subclasses the upstream ChargePoint from ocppv16.py
--------------------------------------------------------------
overrides only the three methods that require hardware-specific behaviour
for the Compleo (innogy) eBox smart (firmware >= 2.3.x, tested on 3.0.4)
and ensures longevity and hardware durability:

    set_charge_rate   - uses TxProfile (transactionId-bound) when a session
                        is active, TxDefaultProfile otherwise; 0 A soft-stop
                        pass-through; 6-16 A clamp for positive values.
    start_transaction - soft-start: 6 A profile confirmed before contactor
                        closes; then a non-blocking background task applies
                        the UI slider value once Charging is confirmed.
    stop_transaction  - soft-stop: 0 A profile + TriggerMessage(MeterValues)
                        + Current.Import poll; up to ~22 s total before
                        RemoteStopTransaction regardless. Both sensor values
                        of current_offered & power_offered are zeroed and the
                        profile slot is cleared.

All other methods (reset, unlock, on_boot_notification, on_meter_values, ...)
are inherited unchanged from ocppv16.ChargePoint.

Strategy - Connector
--------------------
Per the Compleo OCPP 1.6J integration handbook the eBox reports
NumberOfConnectors = 1: it is a single-connector charge point (the eBox smart S
used in development included).  Every override therefore targets connector 1
regardless of the connector_id api.py passes (None, 0 or 1); no multi-connector
bookkeeping is needed.

In a master/slave load-management group each physical eBox keeps its own OCPP
connection (own ChargeBoxID) and still presents only connector 1 - the power
sharing happens over Modbus, invisibly to OCPP - so downstream boxes are never
addressed as connector 2/3 here; they appear as separate charge points.

Strategy - Profile ID
---------------------
The eBox reports MaxChargingProfilesInstalled = 1, i.e. it stores exactly one
charging profile per connector at a time.  Crucially, a TxDefaultProfile and a
TxProfile must NOT share the same chargingProfileId: if a TxProfile is sent
with the same ID as the TxDefaultProfile already occupying the slot, the eBox
ACKs the message but does not actually apply the new limit until a second
same-purpose profile arrives.  This caused current changes to be silently
ignored on the first TxDefault->Tx transition.

Therefore each purpose uses its own ID range:
    TxDefaultProfile -> 2000 + connector_id   (no active session: 6 A soft-start
                                               set before the transaction begins)
    TxProfile        -> 3000 + connector_id   (active session: live current
                                               control AND the 0 A soft-stop,
                                               which still runs while the
                                               transaction is open)

Profile selection is driven purely by whether an active transactionId exists,
not by the requested current.  In particular the 0 A soft-stop in
stop_transaction runs while the transaction is still open, so it uses the
TxProfile slot, not the TxDefaultProfile.

TxProfile for live current control
--------------------------------------
TxDefaultProfile persists across sessions; using it for, e.g. per-minute PV control,
would permanently overwrite the stored limit.  TxProfile is bound to the active
transactionId and is automatically discarded when the session ends.

The prof.SMART feature-profile guard present in the upstream set_charge_rate is
intentionally omitted: the eBox firmware reports SMART and supports it.

Backward-compatibility note
----------------------------
api.py passes keyword arguments to set_charge_rate that this override does not
use (limit_watts, profile).  Both are accepted via **kwargs and ignored.  Watt
values would be rejected by the eBox anyway: per the integration handbook the
ChargingScheduleAllowedChargingRateUnit is "A" only.
"""

import asyncio
import logging

from ocpp.v16 import call
from ocpp.v16.enums import (
    ChargePointStatus,
    ChargingProfileStatus,
    Measurand,
    MessageTrigger,
    RemoteStartStopStatus,
    TriggerMessageStatus,
)

from .enums import HAChargerStatuses as cstat
from .ocppv16 import ChargePoint as _ChargePointV16

_LOGGER: logging.Logger = logging.getLogger(__package__)


class ChargePoint(_ChargePointV16):
    """eBox smart - hardware-aware OCPP 1.6 charge point."""

    def _get_slider_amps(self, connector_id: int) -> float:
        """Read the current target from the UI number slider.

        The slider entity_id follows the pattern
        number.<cpid>_maximum_current (single-connector, flattened).
        Returns the slider value, clamped to 6-16 A, or 6.0 as fallback.
        """
        try:
            entity_id = f"number.{self.settings.cpid}_maximum_current"
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in ("unknown", "unavailable"):
                amps = float(state.state)
                return max(6.0, min(16.0, amps))
        except Exception as ex:
            _LOGGER.debug("eBox: _get_slider_amps could not read slider: %s", ex)
        return 6.0

    async def _apply_target_after_start(self, connector_id: int) -> None:
        """Background task: wait for Charging + active transaction, then apply slider.

        After RemoteStartTransaction is accepted, two asynchronous events still
        need to happen before the real target current can be applied:
          1. The eBox sends StartTransaction.req -> on_start_transaction stores
             the transactionId in self._active_tx (required for TxProfile).
          2. The eBox sends StatusNotification "Charging" -> contactor closed.

        Once both hold, the UI slider value is applied as a TxProfile
        (3000 + connector_id), superseding the 6 A soft-start TxDefaultProfile.
        If the slider is already at 6 A the soft-start level is kept and no
        TxProfile is sent.  Running in the background means the CC switch /
        automation does not block during the IEC 61851 handshake.
        """
        POLL_TIMEOUT = 45    # seconds
        POLL_INTERVAL = 0.5  # seconds

        charging_seen = False
        for _ in range(int(POLL_TIMEOUT / POLL_INTERVAL)):
            await asyncio.sleep(POLL_INTERVAL)

            try:
                active_tx_id = int(self._active_tx.get(connector_id, 0) or 0)
            except Exception:
                active_tx_id = 0
            tx_seen = active_tx_id > 0

            try:
                status = str(
                    self._metrics.get(
                        (connector_id, cstat.status_connector.value),
                    ).value or ""
                )
            except Exception:
                status = ""

            if status == ChargePointStatus.charging.value and not charging_seen:
                charging_seen = True
                _LOGGER.info(
                    "eBox: start_transaction connector status = Charging - "
                    "contactor closed, current flowing (connector=%d)",
                    connector_id,
                )

            if status in (
                ChargePointStatus.available.value,
                ChargePointStatus.faulted.value,
            ):
                _LOGGER.warning(
                    "eBox: start_transaction unexpected connector status '%s' "
                    "while waiting to apply target current (connector=%d)",
                    status, connector_id,
                )
                return

            if charging_seen and tx_seen:
                target_amps = self._get_slider_amps(connector_id)
                if target_amps != 6.0:
                    _LOGGER.info(
                        "eBox: start_transaction applying target current %.1f A "
                        "from UI slider (connector=%d, transactionId=%d)",
                        target_amps, connector_id, active_tx_id,
                    )
                    await self.set_charge_rate(
                        limit_amps=target_amps, conn_id=connector_id
                    )
                else:
                    _LOGGER.debug(
                        "eBox: start_transaction slider at 6 A - keeping "
                        "soft-start level (connector=%d)",
                        connector_id,
                    )
                return

        _LOGGER.warning(
            "eBox: start_transaction timed out waiting for Charging + transaction "
            "(charging=%s) - target current not applied (connector=%d)",
            charging_seen, connector_id,
        )

    # ------------------------------------------------------------------
    # set_charge_rate
    # ------------------------------------------------------------------

    async def set_charge_rate(
        self,
        limit_amps: float = 6.0,
        conn_id: int = 1,
        **kwargs,
    ) -> bool:
        """Set the charge rate on the (single) eBox connector.

        Profile strategy (selected purely by whether a session is active):
          - Active session present  -> TxProfile bound to transactionId,
            chargingProfileId = 3000 + connector_id.
          - No active session       -> TxDefaultProfile,
            chargingProfileId = 2000 + connector_id.

        The two purposes use distinct IDs because the eBox stores only one
        profile (MaxChargingProfilesInstalled = 1) and will not reliably apply
        a TxProfile that re-uses the TxDefaultProfile's ID.

        Current limits:
          - 0 A is passed through unchanged (soft-stop signal).
          - All other values are clamped to the eBox range of 6-16 A.

        conn_id is forced to 1: the eBox is single-connector, so any value
        api.py passes (None, 0 or a stray 2) maps to connector 1.

        The **kwargs absorb legacy keyword arguments forwarded by api.py
        (limit_watts, profile) that are not applicable to the eBox.

        Returns True only when the charger explicitly responds with Accepted.
        """
        amps = float(limit_amps)

        if amps == 0.0:
            clamped = 0.0
        else:
            clamped = max(6.0, min(16.0, amps))

        if clamped != amps:
            _LOGGER.debug("eBox: set_charge_rate clamped %.1f A -> %.1f A", amps, clamped)

        # eBox is single-connector (NumberOfConnectors = 1); every profile
        # targets connector 1 whatever conn_id api.py passes.
        target_cid = 1

        try:
            active_tx_id = int(self._active_tx.get(target_cid, 0) or 0)
        except Exception:
            active_tx_id = 0

        use_tx_profile = active_tx_id > 0

        if use_tx_profile:
            purpose = "TxProfile"
            profile_id = 3000 + target_cid
            _LOGGER.debug(
                "eBox: set_charge_rate %.1f A via TxProfile "
                "(connector=%d, profileId=%d, transactionId=%d)",
                clamped, target_cid, profile_id, active_tx_id,
            )
        else:
            purpose = "TxDefaultProfile"
            profile_id = 2000 + target_cid
            _LOGGER.debug(
                "eBox: set_charge_rate %.1f A via TxDefaultProfile "
                "(connector=%d, profileId=%d, no active session)",
                clamped, target_cid, profile_id,
            )

        schedule = {
            "chargingRateUnit": "A",
            "chargingSchedulePeriod": [{"startPeriod": 0, "limit": clamped}],
        }
        # stackLevel only disambiguates profiles of the SAME purpose.  With
        # MaxChargingProfilesInstalled = 1 only one profile is ever stored, and
        # TxProfile already outranks TxDefaultProfile by purpose precedence, so
        # a fixed stackLevel = 1 is sufficient (handbook allows 0-32).
        profile: dict = {
            "chargingProfileId": profile_id,
            "stackLevel": 1,
            "chargingProfilePurpose": purpose,
            "chargingProfileKind": "Relative",
            "chargingSchedule": schedule,
        }
        if use_tx_profile:
            profile["transactionId"] = active_tx_id

        try:
            req = call.SetChargingProfile(
                connector_id=target_cid,
                cs_charging_profiles=profile,
            )
            resp = await self.call(req)
            if resp.status == ChargingProfileStatus.accepted:
                _LOGGER.debug(
                    "eBox: set_charge_rate %.1f A accepted by charger "
                    "(%s, connector=%d)",
                    clamped, purpose, target_cid,
                )
                return True
            _LOGGER.warning(
                "eBox: set_charge_rate SetChargingProfile rejected: %s", resp.status
            )
        except Exception as ex:
            _LOGGER.warning("eBox: set_charge_rate SetChargingProfile failed: %s", ex)
            await self.notify_ha(
                f"Warning: Set charging profile failed: {ex}"
            )
        return False

    # ------------------------------------------------------------------
    # start_transaction
    # ------------------------------------------------------------------

    async def start_transaction(self, connector_id: int = 1):
        """Remote start a transaction with a soft-start profile.

        Sequence:
          1. Set 6 A TxDefaultProfile so the contactor closes under a
             controlled load rather than full available current.
          2. Retry up to MAX_RETRIES times (RETRY_DELAY s apart); if still not
             accepted, keep re-sending the 6 A profile every POLL_INTERVAL for
             up to CONFIRM_TIMEOUT more seconds.
          3. Send RemoteStartTransaction; return immediately on accept.
          4. A background task (_apply_target_after_start) waits for the
             contactor to close and then applies the UI slider value.

        Current.Import is intentionally not checked here - the contactor is
        still open so no current flows before the transaction starts.

        connector_id is normalized to 1: the eBox is single-connector.
        """
        # eBox is single-connector: always operate on connector 1.
        connector_id = 1

        _LOGGER.info(
            "eBox: start_transaction requesting session on connector %d", connector_id
        )

        MAX_RETRIES = 3
        RETRY_DELAY = 2      # seconds between profile-set attempts
        CONFIRM_TIMEOUT = 9  # seconds to keep re-sending the profile after final retry
        POLL_INTERVAL = 0.5  # seconds between re-send attempts

        _LOGGER.debug(
            "eBox: start_transaction setting 6 A soft-start profile (connector=%d)",
            connector_id,
        )

        profile_accepted = False
        for attempt in range(1, MAX_RETRIES + 1):
            if await self.set_charge_rate(limit_amps=6.0, conn_id=connector_id):
                profile_accepted = True
                _LOGGER.debug(
                    "eBox: start_transaction 6 A profile confirmed on attempt %d/%d",
                    attempt, MAX_RETRIES,
                )
                break
            if attempt < MAX_RETRIES:
                _LOGGER.debug(
                    "eBox: start_transaction profile not accepted on attempt %d/%d "
                    "- retrying in %d s",
                    attempt, MAX_RETRIES, RETRY_DELAY,
                )
                await asyncio.sleep(RETRY_DELAY)

        if not profile_accepted:
            _LOGGER.debug(
                "eBox: start_transaction profile not accepted after %d attempts "
                "- re-sending for up to %d s",
                MAX_RETRIES, CONFIRM_TIMEOUT,
            )
            for _ in range(int(CONFIRM_TIMEOUT / POLL_INTERVAL)):
                await asyncio.sleep(POLL_INTERVAL)
                if await self.set_charge_rate(limit_amps=6.0, conn_id=connector_id):
                    profile_accepted = True
                    _LOGGER.debug(
                        "eBox: start_transaction 6 A profile confirmed during "
                        "confirmation window"
                    )
                    break

        if not profile_accepted:
            _LOGGER.warning(
                "eBox: start_transaction 6 A profile not confirmed within %d s "
                "- sending RemoteStartTransaction anyway",
                RETRY_DELAY * (MAX_RETRIES - 1) + CONFIRM_TIMEOUT,
            )

        _LOGGER.debug(
            "eBox: start_transaction sending RemoteStartTransaction "
            "(connector=%d, id_tag=%s)",
            connector_id, self._remote_id_tag,
        )

        req = call.RemoteStartTransaction(
            connector_id=connector_id, id_tag=self._remote_id_tag
        )
        resp = await self.call(req)

        if resp.status == RemoteStartStopStatus.accepted:
            _LOGGER.info(
                "eBox: start_transaction RemoteStartTransaction accepted "
                "(connector=%d) - scheduling target-current apply in background",
                connector_id,
            )
            self.hass.async_create_task(
                self._apply_target_after_start(connector_id)
            )
            return True

        _LOGGER.warning(
            "eBox: start_transaction failed with response: %s", resp.status
        )
        await self.notify_ha(
            f"Warning: Start transaction failed with response {resp.status}"
        )
        return False

    # ------------------------------------------------------------------
    # stop_transaction
    # ------------------------------------------------------------------

    async def stop_transaction(self, connector_id: int | None = None):
        """Request remote stop of current transaction.

        The eBox is single-connector, so every OCPP call here targets
        connector 1 regardless of the connector_id passed in (None, 0 or 1).

        Sequence:
          1. Resolve the active transactionId (connector 1, with a fallback
             to active_transaction_id).
          2. Set 0 A TxProfile (soft-stop): CP-Pilot duty drops to 0 %,
             IEC 61851 State C->B, contactor opens under zero load.  The
             transaction is still open at this point, so set_charge_rate
             selects the TxProfile slot (3000 + 1), not the TxDefaultProfile.
          3. Send TriggerMessage(MeterValues) for an immediate current reading.
          4. Poll Current.Import until < 0.5 A or CONFIRM_TIMEOUT expires.
          5. Send RemoteStopTransaction to end the OCPP session.
          6. Zero the offered sensors (deferred) and clear the connector's
             profiles so the next session starts clean.
        """
        # eBox is single-connector: always operate on connector 1, whatever
        # connector_id api.py passes (None, 0 or 1).
        cid = 1
        try:
            tx_id = int(
                self._active_tx.get(cid, 0) or self.active_transaction_id or 0
            )
        except (ValueError, TypeError):
            tx_id = 0

        if tx_id == 0:
            _LOGGER.debug("eBox: stop_transaction no active transaction - nothing to do")
            return True

        _LOGGER.info(
            "eBox: stop_transaction stopping transaction %d on connector %d",
            tx_id, cid,
        )

        MAX_RETRIES = 3
        RETRY_DELAY = 2      # seconds between profile-set attempts
        CONFIRM_TIMEOUT = 18 # seconds to poll for Current.Import < 0.5 A
        POLL_INTERVAL = 1    # seconds between current polls

        _LOGGER.debug(
            "eBox: stop_transaction setting 0 A soft-stop profile (connector=%d)", cid
        )

        profile_set = False
        for attempt in range(1, MAX_RETRIES + 1):
            if await self.set_charge_rate(limit_amps=0.0, conn_id=cid):
                profile_set = True
                _LOGGER.debug(
                    "eBox: stop_transaction 0 A profile confirmed on attempt %d/%d "
                    "- contactor opening",
                    attempt, MAX_RETRIES,
                )
                break
            if attempt < MAX_RETRIES:
                _LOGGER.debug(
                    "eBox: stop_transaction 0 A profile not accepted on attempt "
                    "%d/%d - retrying in %d s",
                    attempt, MAX_RETRIES, RETRY_DELAY,
                )
                await asyncio.sleep(RETRY_DELAY)

        if not profile_set:
            _LOGGER.warning(
                "eBox: stop_transaction 0 A profile not accepted after %d attempts "
                "- proceeding anyway to avoid dangling transaction",
                MAX_RETRIES,
            )

        _LOGGER.debug(
            "eBox: stop_transaction sending TriggerMessage(MeterValues) "
            "to get fresh current reading (connector=%d)",
            cid,
        )
        try:
            trig_req = call.TriggerMessage(
                requested_message=MessageTrigger.meter_values,
                connector_id=cid,
            )
            trig_resp = await self.call(trig_req)
            if trig_resp.status == TriggerMessageStatus.accepted:
                _LOGGER.debug(
                    "eBox: stop_transaction TriggerMessage accepted - "
                    "polling Current.Import"
                )
            else:
                _LOGGER.debug(
                    "eBox: stop_transaction TriggerMessage not accepted (%s) "
                    "- relying on poll timeout",
                    trig_resp.status,
                )
        except Exception as ex:
            _LOGGER.debug(
                "eBox: stop_transaction TriggerMessage raised %s - "
                "relying on poll timeout",
                ex,
            )

        current_zero = False
        last_current = 1.0
        for _ in range(int(CONFIRM_TIMEOUT / POLL_INTERVAL)):
            await asyncio.sleep(POLL_INTERVAL)
            try:
                metric = self._metrics.get((cid, Measurand.current_import.value))
                last_current = (
                    float(metric.value)
                    if metric is not None and metric.value is not None
                    else 1.0
                )
            except Exception:
                last_current = 1.0
            if last_current < 0.5:
                _LOGGER.info(
                    "eBox: stop_transaction Current.Import = %.2f A - "
                    "contactor confirmed open (connector=%d)",
                    last_current, cid,
                )
                current_zero = True
                break

        if not current_zero:
            _LOGGER.warning(
                "eBox: stop_transaction Current.Import = %.2f A did not drop below "
                "0.5 A within %d s - proceeding with RemoteStopTransaction",
                last_current, CONFIRM_TIMEOUT,
            )

        _LOGGER.debug(
            "eBox: stop_transaction sending RemoteStopTransaction "
            "(transactionId=%d, connector=%d)",
            tx_id, cid,
        )

        req = call.RemoteStopTransaction(transaction_id=tx_id)
        resp = await self.call(req)

        if resp.status == RemoteStartStopStatus.accepted:
            _LOGGER.info(
                "eBox: stop_transaction session ended "
                "(transactionId=%d, connector=%d)",
                tx_id, cid,
            )
            # The inherited on_stop_transaction zeroes current/power import but
            # NOT current_offered / power_offered.  Those sensors need to be
            # zeroed AFTER on_stop_transaction has completed, because:
            #   (a) the eBox sends StopTransaction.req (with transactionData
            #       containing the last Current.Offered) AFTER we receive the
            #       RemoteStopTransaction.conf here, and
            #   (b) on_stop_transaction overwrites _metrics from that data and
            #       then calls update() - restoring the non-zero value.
            # A deferred task runs 5 s later, safely after on_stop_transaction,
            # zeros the offered metrics and triggers an explicit sensor update.
            _cid = cid  # capture for closure

            async def _zero_offered_deferred() -> None:
                await asyncio.sleep(5)
                _LOGGER.debug(
                    "eBox: stop_transaction zeroing offered sensors "
                    "(connector=%d)",
                    _cid,
                )
                for _meas in (
                    Measurand.current_offered.value,
                    Measurand.power_offered.value,
                ):
                    _key = (_cid, _meas)
                    if _key in self._metrics:
                        self._metrics[_key].value = 0
                self.hass.async_create_task(
                    self.update(self.settings.cpid)
                )

            self.hass.async_create_task(_zero_offered_deferred())
            # Clear the connector's profiles so neither a residual TxProfile
            # (3000+) nor a stale TxDefaultProfile (2000+) carries over into the
            # next session.  clear_profile() is called without a profileId,
            # which per OCPP clears every profile on the connector.
            _LOGGER.debug(
                "eBox: stop_transaction clearing residual profiles (connector=%d)",
                cid,
            )
            await self.clear_profile(conn_id=cid)
            return True

        _LOGGER.warning(
            "eBox: stop_transaction RemoteStopTransaction failed with response: %s",
            resp.status,
        )
        await self.notify_ha(
            f"Warning: Stop transaction failed with response {resp.status}"
        )
        return False
