from setuptools import setup, find_packages

setup(
    name='worker',
    version='0.0.1',
    packages=find_packages(exclude=['tests*']),
    include_package_data=True,
    install_requires=[
        'boto3',
        'hvac',
        'pyhcl',
        'tenacity',
        'cryptography',
        'click',
        'Jinja2',
        'PyYAML',
        'requests'
    ],
    entry_points={
        'console_scripts': [
            'worker=worker.cli:cli'
        ]
    },
    setup_requires=[
        "flake8"
    ]
)
