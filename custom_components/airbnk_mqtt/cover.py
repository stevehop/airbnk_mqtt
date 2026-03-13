class AirbnkCover(CoverEntity):
    _attr_should_poll = False
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE

    # Optional while testing:
    _attr_assumed_state = True

    ...

    @property
    def is_closed(self) -> bool | None:
        """
        Closed when 'locked'; open when 'unlocked'.
        Prefer the parsed text state that sensors use to avoid flip/inversion issues.
        """
        data = getattr(self._device, "_lockData", {})
        text = (data.get("state") or "").strip().lower()
        if text in ("locked", "unlocked"):
            return text == "locked"

        current_state = getattr(self._device, "current_state", None)
        if isinstance(current_state, str) and current_state:
            cs = current_state.strip().lower()
            if cs in ("locked", "unlocked"):
                return cs == "locked"

        state_num = getattr(self._device, "curr_state", None)
        if state_num in (0, 1):
            return state_num == 0

        return None
