import frappe
from frappe import _
from frappe.model.document import Document
from .easypost_api import EasypostAPI, EasypostAPIError


class Package(Document):
    """
    Package Doctype: a Package ;)

    All this are set internally hardcoded. So we can trust in the origin.
    custom flags = {
        'ignore_validate':    Frappe Core Flag if is set avoid: before_validate(), validate() and before_save()
        'requested_to_track': If Package was requested to be tracked we bypass all validations.
        'carrier_can_track':  Carrier can track in API. Comes from related Link to "Package Carrier" Doctype.
        'carrier_uses_utc':   Carrier uses UTC date times. Comes from related Link to "Package Carrier" Doctype.
    }
    """

    def validate(self):
        """ Validate def. We try to detect a valid tracking number. """

        if self.flags.requested_to_track:  # If requested: bypass. We should have validate before set this flag.
            return

        self.tracking_number = self.tracking_number.upper()  # Only uppercase tracking numbers
        tracking_number_strip = self.tracking_number[:3]

        # TODO: What if: What happens in frontend?. Translate spanish?
        if '1Z' in tracking_number_strip and self.carrier != 'UPS':
            frappe.throw('UPS Tracking')
        elif 'TBA' in tracking_number_strip and self.carrier != 'AmazonMws':
            frappe.throw('Amazon Tracking')
        elif any(s in tracking_number_strip for s in ['LY', 'LB']) and self.carrier != 'USPS':
            frappe.throw('Possibly a USPS Tracking')
        elif 'JJD' in tracking_number_strip:
            frappe.throw('Convert to DHL Tracking')

    def before_save(self):
        """ Before is saved on DB, after is validated. Add new data and save once. On Insert(Create) or Save(Update) """
        if self.flags.requested_to_track or (self.is_new() and self.can_track()):  # can_track can't run if is_new=False
            self._request_data_from_easypost_api()  # Track if is requested, or is new and is able to be tracked.
        elif self.has_value_changed('carrier') and self.can_track():  # Already exists and the carrier has changed.
            frappe.msgprint(msg='Carrier has changed, we\'re requesting new data from the API.', title='Carrier Change')
            self.easypost_id = None
            self._request_data_from_easypost_api()
        # TODO: When track is recently set to active!

    def can_track(self):
        """ This def validate if a package can be tracked by any mean using any API, also loads the carrier flags. """
        # TODO: Validate if a tracker API is enabled.

        if not self.track:  # Package is not configured to be tracked, no matter if easypost_id exists.
            frappe.msgprint(msg=_('Package is configured not to track.'), indicator='yellow', alert=True)
            return False

        self.load_carrier_flags()  # Load carrier global flags settings an attach to the document flags.

        if not self.flags.carrier_can_track:  # Carrier is configured to not track. So we don't bother.
            frappe.msgprint(msg=_('Package is handled by a carrier we can\'t track.'), indicator='red', alert=True)
            return False

        return True

    def load_carrier_flags(self):
        """ Loads the carrier global flags settings handling the package in the flags of the Document. """
        self.flags.carrier_can_track, self.flags.carrier_uses_utc = \
            frappe.get_value('Package Carrier', filters=self.carrier, fieldname=['can_track', 'uses_utc'])  # TODO Cache

    def change_status(self, new_status):
        """ Validate the current status of the package and validates if a change is possible. """

        # Package was waiting for receipt, now is mark as delivered. waiting for confirmation.
        # Package was waiting for receipt or confirmation and now is waiting for the departure.
        # Package was not received, and not confirmed, but has appear on the warehouse receipt list

        if self.status != new_status and \
                (self.status == 'Awaiting Receipt' and new_status == 'Awaiting Confirmation') or \
                (self.status in ['Awaiting Receipt', 'Awaiting Confirmation', 'In Extraordinary Confirmation'] and new_status == 'Awaiting Departure') or \
                (self.status in ['Awaiting Receipt', 'Awaiting Confirmation', 'In Extraordinary Confirmation', 'Awaiting Departure'] and new_status == 'In Transit'):
            # TODO: Finish
            print('TRUE . From {0}, To {1}: {2}'.format(self.status, new_status, self.tracking_number))

            self.status = new_status
            return True

        print('FALSE. Is {} was going to {}: {}'.format(self.status, new_status, self.tracking_number))
        return False

    def get_explained_status(self):
        """ This returns a detailed explanation of the current status of the Package and compatible colors. """
        # TODO: one of the best datetime format: "E d LLL yyyy 'at' h:MM a" # TODO: translate this strings.
        color = 'blue'

        if self.status == 'Awaiting Receipt':
            message = ['El transportista aún no ha entregado el paquete.']

            if self.carrier_est_delivery:  # The carrier has provided a estimated delivery date
                message.append(
                    'La fecha prevista es: {}'.format(frappe.utils.format_datetime(self.carrier_est_delivery, 'medium'))
                )
            else:
                color = 'yellow'
                message.append('No se ha indicado una fecha de entrega estimada.')
        elif self.status == 'Awaiting Confirmation' or self.status == 'In Extraordinary Confirmation':
            color = 'yellow'

            if self.carrier_real_delivery:
                message = [
                    'El paquete fue entregado según el transportista el: {}.'.format(
                        frappe.utils.format_datetime(self.carrier_real_delivery, 'medium')
                    )
                ]

                delivered_since = frappe.utils.time_diff_in_seconds(None,self.carrier_real_delivery)  # datetime is UTC

                # Package is no more within the 24 hours timespan to be confirmed. TODO: check against current user tz.
                if (round(delivered_since / 3600, 2) >= 24.00):  # Same as: time_diff_in_hours() >= 24.00
                    message.append('Han pasado: {} y el paquete no ha sido confirmado por el almacén.'.format(
                        frappe.utils.format_duration(delivered_since)
                    ))
                else:
                    color = 'blue'
                    message.append('Por favor espera 24 horas hábiles para que el almacén confirme la recepción.')
            else:
                message = ['El transportista no índico una fecha de entrega.']

            if self.status == 'In Extraordinary Confirmation':
                color = 'yellow'
                message.append('El paquete se encuentra en una verificación fuera de lo habitual.')

        elif self.status == 'Awaiting Departure':
            message = ['El paquete fue recepcionado.', 'Esperando próximo despacho de carga.']
        elif self.status == 'In Transit':
            message = 'El paquete esta en transito a destino.'  # TODO: Add Depature and arrival?
        elif self.status == 'Available to Pickup':
            message = 'El paquete esta listo para ser retirado.'
        elif self.status == 'Finished' or self.status == 'Cancelled':
            return  # No message
        else:
            message, color = 'Contáctese con un agente para obtener mayor información del paquete.', 'yellow'

        return {'message': message, 'color': color}

    def parse_data_from_easypost_webhook(self, response):
        """ Convert a Easypost webhook POST to a Easypost Object, then parses the data to the Document. """
        easypost_api = EasypostAPI(carrier_uses_utc=self.flags.carrier_uses_utc)
        easypost_api.convert_from_webhook(response['result'])  # This convert and normalizes the data

        self._parse_data_from_easypost_instance(easypost_api.instance)

    def _request_data_from_easypost_api(self):
        """ Handles POST or GET to the Easypost API. Also parses the data. """
        try:
            easypost_api = EasypostAPI(carrier_uses_utc=self.flags.carrier_uses_utc)

            if self.easypost_id:  # Package exists on easypost and is requested to be tracked. Request updates from API.
                easypost_api.retrieve_package_data(self.easypost_id)
            else:  # Package don't exists on easypost and is requested to be tracked. We create a new one and attach it.
                easypost_api.create_package(self.tracking_number, self.carrier)

                self.easypost_id = easypost_api.instance.id  # EasyPost ID. Only on creation

        except EasypostAPIError as e:
            frappe.msgprint(msg=str(e), title='EasyPost API Error', raise_exception=False, indicator='red')
            return  # Exit because this has failed(Create or Update)  # FIXME: don't throw because we need to save

        else:  # Data to parse that will be save
            self._parse_data_from_easypost_instance(easypost_api.instance)

            frappe.msgprint(msg=_('Package has been updated from API.'), alert=True)

    def _parse_data_from_easypost_instance(self, instance):
        """ This parses all the data from an easypost instance(with all the details) to our Package Doctype """
        self.carrier_status = instance.status or 'Unknown'
        self.carrier_status_detail = instance.status_detail or 'Unknown'

        self.signed_by = instance.signed_by or None

        self.carrier_est_weight = instance.weight_in_pounds
        self.carrier_est_delivery = instance.naive_est_delivery_date

        # If package is delivered we get the last update details to lookup for the delivery datetime(real delivery date)
        if instance.status == 'Delivered' or instance.status_detail == 'Arrived At Destination':
            self.carrier_real_delivery = EasypostAPI.naive_dt_to_local_dt(instance.tracking_details[-1].datetime, self.flags.carrier_uses_utc)
            self.change_status('Awaiting Confirmation')
        else:  # TODO: Change the status when the carrier status: return_to_sender, failure, cancelled, error
            self.change_status('Awaiting Receipt')

        if instance.tracking_details:
            latest_tracking_details = instance.tracking_details[-1]

            self.carrier_last_detail = "{}\n\n{}\n\n{}".format(
                latest_tracking_details.message,
                latest_tracking_details.description or 'Without Description',
                '{}, {}, {}'.format(  # TODO: Work this out!
                    latest_tracking_details.tracking_location.city,
                    latest_tracking_details.tracking_location.state or '',
                    latest_tracking_details.tracking_location.zip or ''
                )
            )


""" API Methods to communicate with the model that holds our business logic. """


@frappe.whitelist(allow_guest=False)
def get_package_explained_status(source_name: str):
    return frappe.get_doc('Package', source_name, cache=True).get_explained_status()
