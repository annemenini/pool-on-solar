import yaml


class Config:
    def __init__(self, tesla_user_id: str, iaqualink_user_id: str, iaqualink_password: str, path: str = '/app/config/config.yaml'):
        with open(path) as f:
            dict = yaml.load(f, Loader=yaml.FullLoader)
            print(dict)
        # TODO: ugly, use bunchify or protobuf instead
        self.tesla.user_id = tesla_user_id
        self.tesla.cache_file = dict.get('tesla', {}).get('cache_file', '/app/config/cache.json')
        self.iaqualink.user_id = iaqualink_user_id
        self.iaqualink.password = iaqualink_password
        self.iaqualink.pool.devices = dict.get('iaqualink', {}).get('pool', {}).get('devices', [])
        self.iaqualink.pool.activation_excess_power = dict.get('iaqualink', {}).get('pool', {}).get('activation_excess_power', 2000)
        self.iaqualink.pool.deactivation_excess_power = dict.get('iaqualink', {}).get('pool', {}).get('deactivation_excess_power', 500)
        self.iaqualink.pool.operating_window_start = dict.get('iaqualink', {}).get('pool', {}).get('operating_window_start', 8)
        self.iaqualink.pool.operating_window_end = dict.get('iaqualink', {}).get('pool', {}).get('operating_window_end', 18)
        self.iaqualink.lights.devices = dict.get('iaqualink', {}).get('lights', {}).get('devices', [])
        self.iaqualink.lights.minimum_battery = dict.get('iaqualink', {}).get('lights', {}).get('minimum_battery', 50)
        self.iaqualink.lights.operating_window_end = dict.get('iaqualink', {}).get('lights', {}).get('operating_window_end', 22)
        self.location.timezone = dict.get('location', {}).get('timezone', 'UTC')
        self.location.latitude = dict.get('location', {}).get('latitude', 0)
        self.location.longitude = dict.get('location', {}).get('longitude', 0)
        print(self)
