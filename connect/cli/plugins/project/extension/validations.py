import importlib
import inspect
import os
import re
import sys
from collections import deque

import yaml

from connect.cli.plugins.project.validators import (
    get_code_context,
    load_project_toml_file,
    ValidationItem,
    ValidationResult,
)
from connect.cli.plugins.project.extension.utils import get_event_definitions, get_pypi_runner_version
from connect.eaas.core.extension import (
    AnvilExtension,
    EventsExtension,
    Extension,
    WebAppExtension,
)
from connect.eaas.core.responses import (
    CustomEventResponse,
    ProcessingResponse,
    ProductActionResponse,
    ValidationResponse,
)


def validate_pyproject_toml(config, project_dir, context):  # noqa: CCR001
    messages = []

    data = load_project_toml_file(project_dir)
    if isinstance(data, ValidationResult):
        return data

    descriptor_file = os.path.join(project_dir, 'pyproject.toml')
    dependencies = data['tool']['poetry']['dependencies']

    if 'connect-extension-runner' in dependencies:
        messages.append(
            ValidationItem(
                'WARNING',
                'Extensions must depend on *connect-eaas-core* library not *connect-extension-runner*.',
                descriptor_file,
            ),
        )
    elif 'connect-eaas-core' not in dependencies:
        messages.append(
            ValidationItem(
                'ERROR',
                'No dependency on *connect-eaas-core* has been found.',
                descriptor_file,
            ),
        )

    extension_dict = data['tool']['poetry'].get('plugins', {}).get('connect.eaas.ext')
    if not isinstance(extension_dict, dict):
        messages.append(
            ValidationItem(
                'ERROR',
                (
                    'No extension declaration has been found.'
                    'The extension must be declared in the *[tool.poetry.plugins."connect.eaas.ext"]* section.'
                ),
                descriptor_file,
            ),
        )
        return ValidationResult(messages, True)

    sys.path.append(os.path.join(os.getcwd(), project_dir))
    possible_extensions = ['extension', 'webapp', 'anvil']
    extensions = {}
    for extension_type in possible_extensions:
        if extension_type in extension_dict.keys():
            package, class_name = extension_dict[extension_type].rsplit(':', 1)
            try:
                extension_module = importlib.import_module(package)
            except ImportError as err:
                messages.append(
                    ValidationItem(
                        'ERROR',
                        f'The extension class *{extension_dict[extension_type]}* '
                        f'cannot be loaded: {err}.',
                        descriptor_file,
                    ),
                )
                return ValidationResult(messages, True)

            defined_classes = [
                member[1]
                for member in inspect.getmembers(extension_module, predicate=inspect.isclass)
            ]

            for deprecated_cls, cls_name in (
                (CustomEventResponse, 'InteractiveResponse'),
                (ProcessingResponse, 'BackgroundResponse'),
                (ProductActionResponse, 'InteractiveResponse'),
                (ValidationResponse, 'InteractiveResponse'),
            ):
                if deprecated_cls in defined_classes:
                    messages.append(
                        ValidationItem(
                            'WARNING',
                            f'The response class *{deprecated_cls.__name__}* '
                            f'has been deprecated in favor of *{cls_name}*.',
                            **get_code_context(extension_module, deprecated_cls.__name__),
                        ),
                    )

            extensions[extension_type] = getattr(extension_module, class_name)

    if not extensions:
        messages.append(
            ValidationItem(
                'ERROR',
                (
                    'Invalid extension declaration in *[tool.poetry.plugins."connect.eaas.ext"]*: '
                    'The extension must be declared as: *"extension" = "your_package.extension:YourExtension"* '
                    'for Fulfillment automation or Hub integration. For Multi account installation must be '
                    'declared at least one the following: *"extension" = "your_package.events:YourEventsExtension"*, '
                    '*"webapp" = "your_package.webapp:YourWebAppExtension"*, '
                    '*"anvil" = "your_package.anvil:YourAnvilExtension"*.'
                ),
                descriptor_file,
            ),
        )
        return ValidationResult(messages, True)

    return ValidationResult(messages, False, {'extension_classes': extensions})


def validate_extension_class(config, project_dir, context):  # noqa: CCR001
    messages = []
    class_mapping = {
        'extension': 'connect.eaas.core.extension.[Events]Extension',
        'webapp': 'connect.eaas.core.extension.WebAppExtension',
        'anvil': 'connect.eaas.core.extension.AnvilExtension',
    }
    ext_class = None
    extension_json_file = None

    for extension_type, extension_class in context['extension_classes'].items():
        extension_class_file = inspect.getsourcefile(extension_class)

        if (
            extension_type == 'extension'
            and not issubclass(extension_class, (Extension, EventsExtension))
            or extension_type == 'webapp' and not issubclass(extension_class, WebAppExtension)
            or extension_type == 'anvil' and not issubclass(extension_class, AnvilExtension)
        ):
            messages.append(
                ValidationItem(
                    'ERROR',
                    f'The extension class *{extension_class.__name__}* '
                    f'is not a subclass of *{class_mapping[extension_type]}*.',
                    extension_class_file,
                ),
            )
            return ValidationResult(messages, True)

        if not extension_json_file:
            extension_json_file = os.path.join(os.path.dirname(extension_class_file), 'extension.json')
            ext_class = extension_class

    try:
        descriptor = ext_class.get_descriptor()
    except FileNotFoundError:
        messages.append(
            ValidationItem(
                'ERROR',
                'The extension descriptor *extension.json* cannot be loaded.',
                extension_json_file,
            ),
        )
        return ValidationResult(messages, True)

    for description in ['variables', 'capabilities', 'schedulables']:
        if description in descriptor:
            messages.append(
                ValidationItem(
                    'WARNING',
                    f'Extension {description} must be declared using the '
                    f'*connect.eaas.core.decorators.'
                    f'{description if description != "schedulables" else "event"}* decorator.',
                    extension_json_file,
                ),
            )

    return ValidationResult(messages, False, {'descriptor': descriptor})


def validate_events(config, project_dir, context):
    messages = []

    extension_class = context['extension_classes'].get('extension')

    if not extension_class:
        return ValidationResult(messages, False, context)

    definitions = {definition['type']: definition for definition in get_event_definitions(config)}
    events = extension_class.get_events()
    for event in events:
        method = getattr(extension_class, event['method'])
        if event['event_type'] not in definitions:
            messages.append(
                ValidationItem(
                    'ERROR',
                    f'The event type *{event["event_type"]}* is not valid.',
                    **get_code_context(method, '@event'),
                ),
            )
            continue

        if definitions[event['event_type']]['object_statuses']:
            invalid_statuses = set(event['statuses']) - set(definitions[event['event_type']]['object_statuses'])
        else:
            invalid_statuses = set(event['statuses'] or [])
        if invalid_statuses:
            messages.append(
                ValidationItem(
                    'ERROR',
                    f'The status/es *{", ".join(invalid_statuses)}* are invalid '
                    f'for the event *{event["event_type"]}*.',
                    **get_code_context(method, '@event'),
                ),
            )

        signature = inspect.signature(method)
        if len(signature.parameters) != 2:
            sig_str = f'{event["method"]}({", ".join(signature.parameters)})'

            messages.append(
                ValidationItem(
                    'ERROR',
                    f'The handler for the event *{event["event_type"]}* has an invalid signature: *{sig_str}*',
                    **get_code_context(method, sig_str),
                ),
            )
    return ValidationResult(messages, False, context)


def validate_schedulables(config, project_dir, context):
    messages = []

    extension_class = context['extension_classes'].get('extension')

    if not extension_class:
        return ValidationResult(messages, False, context)

    schedulables = extension_class.get_schedulables()
    for schedulable in schedulables:
        method = getattr(extension_class, schedulable['method'])
        signature = inspect.signature(method)
        if len(signature.parameters) != 2:
            sig_str = f'{schedulable["method"]}({", ".join(signature.parameters)})'

            messages.append(
                ValidationItem(
                    'ERROR',
                    f'The schedulable method *{schedulable["method"]}* has an invalid signature: *{sig_str}*',
                    **get_code_context(method, sig_str),
                ),
            )
    return ValidationResult(messages, False, context)


def validate_variables(config, project_dir, context):  # noqa: CCR001

    messages = []

    for _, extension_class in context['extension_classes'].items():

        variables = extension_class.get_variables()
        variable_name_pattern = r'^[A-Za-z](?:[A-Za-z0-9_\-.]+)*$'
        variable_name_regex = re.compile(variable_name_pattern)

        names = []

        for variable in variables:
            if 'name' not in variable:
                messages.append(
                    ValidationItem(
                        'ERROR',
                        'Invalid variable declaration: the *name* attribute is mandatory.',
                        **get_code_context(extension_class, '@variables'),
                    ),
                )
                continue

            if variable["name"] in names:
                messages.append(
                    ValidationItem(
                        'ERROR',
                        f'Duplicate variable name: the variable with name *{variable["name"]}* '
                        'has already been declared.',
                        **get_code_context(extension_class, '@variables'),
                    ),
                )

            names.append(variable["name"])

            if not variable_name_regex.match(variable['name']):
                messages.append(
                    ValidationItem(
                        'ERROR',
                        f'Invalid variable name: the value *{variable["name"]}* '
                        f'does not match the pattern *{variable_name_pattern}*.',
                        **get_code_context(extension_class, '@variables'),
                    ),
                )
            if 'initial_value' in variable and not isinstance(variable['initial_value'], str):
                messages.append(
                    ValidationItem(
                        'ERROR',
                        f'Invalid *initial_value* attribute for variable *{variable["name"]}*: '
                        f'must be a non-null string not *{type(variable["initial_value"])}*.',
                        **get_code_context(extension_class, '@variables'),
                    ),
                )

            if 'secure' in variable and not isinstance(variable['secure'], bool):
                messages.append(
                    ValidationItem(
                        'ERROR',
                        f'Invalid *secure* attribute for variable *{variable["name"]}*: '
                        f'must be a boolean not *{type(variable["secure"])}*.',
                        **get_code_context(extension_class, '@variables'),
                    ),
                )

    return ValidationResult(messages, False, context)


def validate_webapp_extension(config, project_dir, context):  # noqa: CCR001

    messages = []

    if 'webapp' not in context['extension_classes']:
        return ValidationResult(messages, False, context)

    extension_class = context['extension_classes']['webapp']
    extension_class_file = inspect.getsourcefile(extension_class)

    if not inspect.getsource(extension_class).strip().startswith('@web_app(router)'):
        messages.append(
            ValidationItem(
                'ERROR',
                'The Web app extension class must be wrapped in *@web_app(router)*.',
                extension_class_file,
            ),
        )
        return ValidationResult(messages, True)

    has_router_function = False
    for _, value in inspect.getmembers(extension_class):
        if (
                inspect.isfunction(value)
                and inspect.getsource(value).strip().startswith('@router.')
        ):
            has_router_function = True
            break

    if not has_router_function:
        messages.append(
            ValidationItem(
                'ERROR',
                'The Web app extension class must contain at least one router '
                'implementation function wrapped in *@router.your_method("/your_path")*.',
                extension_class_file,
            ),
        )
        return ValidationResult(messages, True)

    if 'ui' not in context['descriptor']:
        messages.append(
            ValidationItem(
                'ERROR',
                'The extension descriptor *extension.json* must contain information '
                'about static files. Please use *ui* keyword, to define an item '
                'use *label* for name and *url* to specify absolute path to file within '
                'static root folder. For more information, look at example: '
                'https://github.com/cloudblue/eaas-e2e-ma-mock/blob/master/e2e/extension.json.',
                extension_class_file,
            ),
        )
        return ValidationResult(messages, True)

    ui_items = deque()
    missed_files = []
    for _, value in context['descriptor']['ui'].items():
        ui_items.append(value)

    while ui_items:
        ui_item = ui_items.pop()
        try:
            url = ui_item['url']
        except KeyError:
            messages.append(
                ValidationItem(
                    'ERROR',
                    'The extension descriptor *extension.json* contains incorrect '
                    f'ui item *{ui_item.get("label")}*, url is not presented.',
                    extension_class_file,
                ),
            )
            return ValidationResult(messages, True)

        path = os.path.join(os.path.dirname(extension_class_file), url.strip('/'))
        if not os.path.exists(path):
            missed_files.append(url)

        for child in ui_item.get('children', []):
            ui_items.append(child)

    if missed_files:
        messages.append(
            ValidationItem(
                'ERROR',
                'The extension descriptor *extension.json* contains missing '
                f'static files: {missed_files}.',
                extension_class_file,
            ),
        )
        return ValidationResult(messages, True)

    return ValidationResult(messages, False, context)


def validate_anvil_extension(config, project_dir, context):

    messages = []

    if 'anvil' not in context['extension_classes']:
        return ValidationResult(messages, False, context)

    # check that anvil variables are correctly specified ?

    return ValidationResult(messages, False, context)


def validate_docker_compose_yml(config, project_dir, context):
    messages = []
    compose_file = os.path.join(project_dir, 'docker-compose.yml')
    if not os.path.isfile(compose_file):
        messages.append(
            ValidationItem(
                'WARNING',
                (
                    f'The directory *{project_dir}* does not look like an extension project directory, '
                    'the file *docker-compose.yml* is not present.'
                ),
                compose_file,
            ),
        )
        return ValidationResult(messages, False)
    try:
        data = yaml.safe_load(open(compose_file, 'r'))
    except yaml.YAMLError:
        messages.append(
            ValidationItem(
                'ERROR',
                'The file *docker-compose.yml* is not valid.',
                compose_file,
            ),
        )
        return ValidationResult(messages, False)

    runner_image = f'cloudblueconnect/connect-extension-runner:{get_pypi_runner_version()}'

    for service in data['services']:
        image = data['services'][service].get('image')
        if image != runner_image:
            messages.append(
                ValidationItem(
                    'ERROR',
                    f'Invalid image for service *{service}*: expected *{runner_image}* got *{image}*.',
                    compose_file,
                ),
            )
    return ValidationResult(messages, False)


validators = [
    validate_pyproject_toml,
    validate_docker_compose_yml,
    validate_extension_class,
    validate_events,
    validate_variables,
    validate_schedulables,
    validate_webapp_extension,
    validate_anvil_extension,
]
