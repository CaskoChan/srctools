from setuptools import setup, Extension, find_packages
import sys
import os

try:
    from Cython.Build import cythonize
    cy_ext = '.pyx'
except ImportError:
    print('Cython not installed, not compiling Cython modules.')
    cy_ext = '.c'
    def cythonize(mod):
        return mod

WIN = sys.platform.startswith('win')

SQUISH_CPP = [
    'libsquish/alpha.cpp',
    'libsquish/clusterfit.cpp',
    'libsquish/colourblock.cpp',
    'libsquish/colourfit.cpp',
    'libsquish/colourset.cpp',
    'libsquish/maths.cpp',
    'libsquish/rangefit.cpp',
    'libsquish/singlecolourfit.cpp',
    'libsquish/squish.cpp',
]

setup(
    name='srctools',
    version='1.2.0',
    description="Modules for working with Valve's Source Engine file formats.",
    url='https://github.com/TeamSpen210/srctools',

    author='TeamSpen210',
    author_email='spencerb21@live.com',
    license='unlicense',

    keywords='',
    classifiers=[
        'License :: Public Domain',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3 :: Only',
    ],
    packages=find_packages(include=['srctools', 'srctools.*']),
    # Setuptools automatically runs Cython, if available.
    ext_modules=cythonize([
        Extension(
            "srctools._tokenizer",
            sources=["srctools/_tokenizer" + cy_ext],
            # extra_compile_args=['/FAs'],  # MS ASM dump
        ),
        Extension(
            "srctools._cy_vtf_readwrite",
            include_dirs=[os.path.abspath("libsquish/")],
            language='c++',
            sources=[
                "srctools/_cy_vtf_readwrite" + cy_ext,
            ] + SQUISH_CPP,
            extra_compile_args=[
                '/openmp' if WIN else '-openmp',
                 # '/FAs',  # MS ASM dump
            ],
            extra_link_args=['/openmp' if WIN else '-openmp'],
        ),
        Extension(
            "srctools._vec",
            sources=["srctools/_vec" + cy_ext],
            # extra_compile_args=['/FAs'],  # MS ASM dump
        ),
    ]),

    package_data={'srctools': [
        'fgd.lzma',
        'srctools.fgd',
        'py.typed',
    ]},

    entry_points={
        'console_scripts': [
            'srctools_dump_parms = srctools.scripts.dump_parms:main',
            'srctools_diff = srctools.scripts.diff:main',
        ],
        'pyinstaller40': [
            'hook-dirs = srctools._pyinstaller:get_hook_dirs',
        ]
    },
    python_requires='>=3.6, <4',
    install_requires=[
        'importlib_resources',
    ],
)
