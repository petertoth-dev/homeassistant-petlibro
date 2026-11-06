from homeassistant.exceptions import HomeAssistantError


class PetLibroAPIError(HomeAssistantError):
    "Basic API error"


class PetLibroCannotConnect(PetLibroAPIError):
    """Error to indicate we cannot connect."""


class PetLibroInvalidAuth(PetLibroAPIError):
    """Error to indicate there is invalid auth."""


class PetLibroLoginRequired(PetLibroInvalidAuth):
    """The session ended and no password is stored: the user has to log in again."""
