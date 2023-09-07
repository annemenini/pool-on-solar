# pool-on-solar

Automate the activation / deactivation of the pool cleaning system so that it only runs when excess solar power is available.

Requirements:

* [Tesla Energy system](https://www.tesla.com/energy) (solar panels, and optionally powerwall)
* [Jandy iAquaLink](https://www.jandy.com/en/products/controls/system-components/interfaces/iaqualink) (pool pump controls)

## Configuration

Set the following env vars:

- `CONFIG_FILE`: location of the pbtxt config file (see automation/config.proto for the config definition)
- `TESLA_USER_ID`: user email of your Tesla account
- `IAQUALINK_USER_ID`: user email of your iAquaLink account 
- `IAQUALINK_PASSWORD`: password of your iAquaLink account, we recommend storing this in a secret
- `ENERGY_SITE_ID`: (optional) ID of the tesla energy site (save an API call and decrease failure rate)
