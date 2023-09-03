# pool-on-solar

Automate the activation / deactivation of the pool cleaning system so that it only runs when excess solar power is available.

Requirements:

* [Tesla Energy system](https://www.tesla.com/energy) (solar panels, and optionally powerwall)
* [Jandy iAquaLink](https://www.jandy.com/en/products/controls/system-components/interfaces/iaqualink) (pool pump controls)

## Configuration

Set the following env vars:

- `TESLA_USER_ID`: user email of your Tesla account
- `TESLAPY_CACHE_FILE`: path to the `cache.json` file created after a first authentication via TeslaPy. We recommend storing this file in a secret and mounting it in your container. For example `/app/config/cache.json`.
- `IAQUALINK_USER_ID`: user email of your iAquaLink account 
- `IAQUALINK_PASSWORD`: password of your iAquaLink account, we recommend storing this in a secret
