'''
Hartree-Fock for periodic systems at a single k-point

See Also:
    pyscf.pbc.scf.khf.py : Hartree-Fock for periodic systems with k-point sampling
'''

import sys
import time
import numpy as np
import h5py
import pyscf.gto
import pyscf.scf.hf
from pyscf import lib
from pyscf.lib import logger
from pyscf.scf.hf import make_rdm1
from pyscf.pbc import tools
from pyscf.pbc.gto import pseudo, ewald
from pyscf.pbc.scf import chkfile
from pyscf.pbc.scf import addons


def get_ovlp(cell, kpt=np.zeros(3)):
    '''Get the overlap AO matrix.
    '''
    return cell.pbc_intor('cint1e_ovlp_sph', hermi=1, kpts=kpt)


def get_hcore(cell, kpt=np.zeros(3)):
    '''Get the core Hamiltonian AO matrix.
    '''
    hcore = get_t(cell, kpt)
    if cell.pseudo:
        hcore += get_pp(cell, kpt) + get_jvloc_G0(cell, kpt)
    else:
        hcore += get_nuc(cell, kpt)

    return hcore


def get_t(cell, kpt=np.zeros(3)):
    '''Get the kinetic energy AO matrix.
    '''
    return cell.pbc_intor('cint1e_kin_sph', hermi=1, kpts=kpt)


def get_nuc(cell, kpt=np.zeros(3)):
    '''Get the bare periodic nuc-el AO matrix, with G=0 removed.

    See Martin (12.16)-(12.21).
    '''
    from pyscf.pbc.dft import gen_grid
    from pyscf.pbc.dft import numint
    coords = gen_grid.gen_uniform_grids(cell)
    aoR = numint.eval_ao(cell, coords, kpt)

    chargs = cell.atom_charges()
    SI = cell.get_SI()
    coulG = tools.get_coulG(cell)
    vneG = -np.dot(chargs,SI) * coulG
    vneR = tools.ifft(vneG, cell.gs).real

    vne = np.dot(aoR.T.conj(), vneR.reshape(-1,1)*aoR)
    return vne

def get_pp(cell, kpt=np.zeros(3)):
    '''Get the periodic pseudotential nuc-el AO matrix, with G=0 removed.
    '''
    import pyscf.dft
    from pyscf.pbc.dft import gen_grid
    from pyscf.pbc.dft import numint
    coords = gen_grid.gen_uniform_grids(cell)
    aoR = numint.eval_ao(cell, coords, kpt)
    nao = cell.nao_nr()

    SI = cell.get_SI()
    vlocG = pseudo.get_vlocG(cell)
    vpplocG = -np.sum(SI * vlocG, axis=0)

    # vpploc evaluated in real-space
    vpplocR = tools.ifft(vpplocG, cell.gs).real
    vpploc = np.dot(aoR.T.conj(), vpplocR.reshape(-1,1)*aoR)

    # vppnonloc evaluated in reciprocal space
    aokG = tools.map_fftk(aoR, cell.gs, np.exp(-1j*np.dot(coords, kpt)))
    ngs = len(aokG)

    fakemol = pyscf.gto.Mole()
    fakemol._atm = np.zeros((1,pyscf.gto.ATM_SLOTS), dtype=np.int32)
    fakemol._bas = np.zeros((1,pyscf.gto.BAS_SLOTS), dtype=np.int32)
    ptr = pyscf.gto.PTR_ENV_START
    fakemol._env = np.zeros(ptr+10)
    fakemol._bas[0,pyscf.gto.NPRIM_OF ] = 1
    fakemol._bas[0,pyscf.gto.NCTR_OF  ] = 1
    fakemol._bas[0,pyscf.gto.PTR_EXP  ] = ptr+3
    fakemol._bas[0,pyscf.gto.PTR_COEFF] = ptr+4
    Gv = np.asarray(cell.Gv+kpt)
    G_rad = lib.norm(Gv, axis=1)

    vppnl = np.zeros((nao,nao), dtype=np.complex128)
    for ia in range(cell.natm):
        symb = cell.atom_symbol(ia)
        if symb not in cell._pseudo:
            continue
        pp = cell._pseudo[symb]
        for l, proj in enumerate(pp[5:]):
            rl, nl, hl = proj
            if nl > 0:
                hl = np.asarray(hl)
                fakemol._bas[0,pyscf.gto.ANG_OF] = l
                fakemol._env[ptr+3] = .5*rl**2
                fakemol._env[ptr+4] = rl**(l+1.5)*np.pi**1.25
                pYlm_part = pyscf.dft.numint.eval_ao(fakemol, Gv, deriv=0)

                pYlm = np.empty((nl,l*2+1,ngs))
                for k in range(nl):
                    qkl = pseudo.pp._qli(G_rad*rl, l, k)
                    pYlm[k] = pYlm_part.T * qkl
                # pYlm is real
                SPG_lmi = np.einsum('g,nmg->nmg', SI[ia].conj(), pYlm)
                SPG_lm_aoG = np.einsum('nmg,gp->nmp', SPG_lmi, aokG)
                tmp = np.einsum('ij,jmp->imp', hl, SPG_lm_aoG)
                vppnl += np.einsum('imp,imq->pq', SPG_lm_aoG.conj(), tmp)
    vppnl *= (1./ngs**2)

    if aoR.dtype == np.double:
        return vpploc.real + vppnl.real
    else:
        return vpploc + vppnl


def get_jvloc_G0(cell, kpt=np.zeros(3)):
    '''Get the (separately divergent) Hartree + Vloc G=0 contribution.
    '''
    return 1./cell.vol * np.sum(pseudo.get_alphas(cell)) * get_ovlp(cell, kpt)


def get_j(cell, dm, hermi=1, vhfopt=None, kpt=np.zeros(3), kpt_band=None):
    '''Get the Coulomb (J) AO matrix for the given density matrix.

    Args:
        dm : ndarray or list of ndarrays
            A density matrix or a list of density matrices

    Kwargs:
        hermi : int
            Whether J, K matrix is hermitian
            | 0 : no hermitian or symmetric
            | 1 : hermitian
            | 2 : anti-hermitian
        vhfopt :
            A class which holds precomputed quantities to optimize the
            computation of J, K matrices
        kpt : (3,) ndarray
            The "inner" dummy k-point at which the DM was evaluated (or
            sampled).
        kpt_band : (3,) ndarray
            The "outer" primary k-point at which J and K are evaluated.

    Returns:
        The function returns one J matrix, corresponding to the input
        density matrix (both order and shape).
    '''
    from pyscf.pbc.dft import gen_grid
    from pyscf.pbc.dft import numint
    dm = np.asarray(dm)
    nao = dm.shape[-1]

    coords = gen_grid.gen_uniform_grids(cell)
    if kpt_band is None:
        kpt1 = kpt2 = kpt
        aoR_k1 = aoR_k2 = numint.eval_ao(cell, coords, kpt)
    else:
        kpt1 = kpt_band
        kpt2 = kpt
        aoR_k1 = numint.eval_ao(cell, coords, kpt1)
        aoR_k2 = numint.eval_ao(cell, coords, kpt2)
    ngs, nao = aoR_k1.shape

    def contract(dm):
        vjR_k2 = get_vjR(cell, dm, aoR_k2)
        vj = (cell.vol/ngs) * np.dot(aoR_k1.T.conj(), vjR_k2.reshape(-1,1)*aoR_k1)
        return vj

    if dm.ndim == 2:
        vj = contract(dm)
    else:
        vj = lib.asarray([contract(x) for x in dm.reshape(-1,nao,nao)])
    return vj.reshape(dm.shape)


def get_jk(mf, cell, dm, hermi=1, vhfopt=None, kpt=np.zeros(3), kpt_band=None):
    '''Get the Coulomb (J) and exchange (K) AO matrices for the given density matrix.

    Args:
        dm : ndarray or list of ndarrays
            A density matrix or a list of density matrices

    Kwargs:
        hermi : int
            Whether J, K matrix is hermitian
            | 0 : no hermitian or symmetric
            | 1 : hermitian
            | 2 : anti-hermitian
        vhfopt :
            A class which holds precomputed quantities to optimize the
            computation of J, K matrices
        kpt : (3,) ndarray
            The "inner" dummy k-point at which the DM was evaluated (or
            sampled).
        kpt_band : (3,) ndarray
            The "outer" primary k-point at which J and K are evaluated.

    Returns:
        The function returns one J and one K matrix, corresponding to the input
        density matrix (both order and shape).
    '''
    from pyscf.pbc.dft import gen_grid
    from pyscf.pbc.dft import numint
    dm = np.asarray(dm)
    nao = dm.shape[-1]

    coords = gen_grid.gen_uniform_grids(cell)
    if kpt_band is None:
        kpt1 = kpt2 = kpt
        aoR_k1 = aoR_k2 = numint.eval_ao(cell, coords, kpt)
    else:
        kpt1 = kpt_band
        kpt2 = kpt
        aoR_k1 = numint.eval_ao(cell, coords, kpt1)
        aoR_k2 = numint.eval_ao(cell, coords, kpt2)

    vkR_k1k2 = get_vkR(mf, cell, aoR_k1, aoR_k2, kpt1, kpt2)

    ngs, nao = aoR_k1.shape
    def contract(dm):
        vjR_k2 = get_vjR(cell, dm, aoR_k2)
        vj = (cell.vol/ngs) * np.dot(aoR_k1.T.conj(), vjR_k2.reshape(-1,1)*aoR_k1)

        #:vk = (cell.vol/ngs) * np.einsum('rs,Rp,Rqs,Rr->pq', dm, aoR_k1.conj(),
        #:                                vkR_k1k2, aoR_k2)
        aoR_dm_k2 = np.dot(aoR_k2, dm)
        tmp_Rq = np.einsum('Rqs,Rs->Rq', vkR_k1k2, aoR_dm_k2)
        vk = (cell.vol/ngs) * np.dot(aoR_k1.T.conj(), tmp_Rq)
        return vj, vk

    if dm.ndim == 2:
        vj, vk = contract(dm)
    else:
        jk = [contract(x) for x in dm.reshape(-1,nao,nao)]
        vj = lib.asarray([x[0] for x in jk])
        vk = lib.asarray([x[1] for x in jk])
    return vj.reshape(dm.shape), vk.reshape(dm.shape)


def get_vjR(cell, dm, aoR):
    '''Get the real-space Hartree potential of the given density matrix.

    Returns:
        vR : (ngs,) ndarray
            The real-space Hartree potential at every grid point.
    '''
    from pyscf.pbc.dft import numint
    coulG = tools.get_coulG(cell)

    rhoR = numint.eval_rho(cell, aoR, dm)
    rhoG = tools.fft(rhoR, cell.gs)

    vG = coulG*rhoG
    vR = tools.ifft(vG, cell.gs)
    if rhoR.dtype == np.double:
        vR = vR.real
    return vR


def get_vkR(mf, cell, aoR_k1, aoR_k2, kpt1, kpt2):
    '''Get the real-space 2-index "exchange" potential V_{i,k1; j,k2}(r)
    where {i,k1} = exp^{i k1 r) |i> , {j,k2} = exp^{-i k2 r) <j|

    Kwargs:
        kpt1, kpt2 : (3,) ndarray
            The sampled k-points; may be required for G=0 correction.

    Returns:
        vR : (ngs, nao, nao) ndarray
            The real-space "exchange" potential at every grid point, for all
            AO pairs.

    Note:
        This is essentially a density-fitting or resolution-of-the-identity.
        The returned object is of size ngs*nao**2 and could be precomputed and
        saved in vhfopt.
    '''
    from pyscf.pbc.dft import gen_grid
    coords = gen_grid.gen_uniform_grids(cell)
    ngs, nao = aoR_k1.shape

    expmikr = np.exp(-1j*np.dot(kpt1-kpt2,coords.T))
    coulG = tools.get_coulG(cell, kpt1-kpt2, exx=True, mf=mf)
    def prod(ij):
        i, j = divmod(ij, nao)
        rhoR = aoR_k1[:,i] * aoR_k2[:,j].conj()
        rhoG = tools.fftk(rhoR, cell.gs, expmikr)
        vG = coulG*rhoG
        vR = tools.ifftk(vG, cell.gs, expmikr.conj())
        return vR

    if aoR_k1.dtype == np.double and aoR_k2.dtype == np.double:
        vR = tools.pbc._map(prod, ngs, nao**2).real
    else:
        vR = tools.pbc._map(prod, ngs, nao**2)
    return vR.reshape(-1,nao,nao)

#def get_veff(mf, cell, dm, dm_last=0, vhf_last=0, hermi=1, vhfopt=None,
#             kpt=np.zeros(3), kpt_band=None):
#    '''Hartree-Fock potential matrix for the given density matrix.
#    See :func:`scf.hf.get_veff` and :func:`scf.hf.RHF.get_veff`
#    '''
#    vj, vk = mf.get_jk(cell, dm, hermi, kpt, kpt_band)
#    return vj - vk * .5

def get_bands(mf, kpt_band, cell=None, dm=None, kpt=None):
    '''Get energy bands at a given (arbitrary) 'band' k-point.

    Returns:
        mo_energy : (nao,) ndarray
            Bands energies E_n(k)
        mo_coeff : (nao, nao) ndarray
            Band orbitals psi_n(k)
    '''
    if cell is None: cell = mf.cell
    if dm is None: dm = mf.make_rdm1()
    if kpt is None: kpt = mf.kpt

    fock = (mf.get_hcore(kpt=kpt_band) +
            mf.get_veff(cell, dm, kpt=kpt, kpt_band=kpt_band))
    s1e = mf.get_ovlp(kpt=kpt_band)
    mo_energy, mo_coeff = mf.eig(fock, s1e)
    return mo_energy, mo_coeff


def init_guess_by_chkfile(cell, chkfile_name, project=True, kpt=None):
    '''Read the HF results from checkpoint file, then project it to the
    basis defined by ``cell``

    Returns:
        Density matrix, (nao,nao) ndarray
    '''
    chk_cell, scf_rec = chkfile.load_scf(chkfile_name)
    mo = scf_rec['mo_coeff']
    mo_occ = scf_rec['mo_occ']
    if kpt is None:
        kpt = np.zeros(3)
    if 'kpt' in scf_rec:
        chk_kpt = scf_rec['kpt']
    elif 'kpts' in scf_rec:
        kpts = scf_rec['kpts'] # the closest kpt from KRHF results
        where = np.argmin(lib.norm(kpts-kpt, axis=1))
        chk_kpt = kpts[where]
        if mo.ndim == 3:  # KRHF:
            mo = mo[where]
            mo_occ = mo_occ[where]
        else:
            mo = mo[:,where]
            mo_occ = mo_occ[:,where]
    else:  # from molecular code
        chk_kpt = np.zeros(3)

    def fproj(mo):
        if project:
            return addons.project_mo_nr2nr(chk_cell, mo, cell, chk_kpt-kpt)
        else:
            return mo
    if mo.ndim == 2:
        dm = make_rdm1(fproj(mo), mo_occ)
    else:  # UHF
        dm =(make_rdm1(fproj(mo[0]), mo_occ[0]) +
             make_rdm1(fproj(mo[1]), mo_occ[1]))

    # Real DM for gamma point
    if kpt is None or np.allclose(kpt, 0):
        dm = dm.real
    return dm


def dot_eri_dm(eri, dm, hermi=0):
    '''Compute J, K matrices in terms of the given 2-electron integrals and
    density matrix. eri or dm can be complex.

    Args:
        eri : ndarray
            complex integral array with N^4 elements (N is the number of orbitals)
        dm : ndarray or list of ndarrays
            A density matrix or a list of density matrices

    Kwargs:
        hermi : int
            Whether J, K matrix is hermitian

            | 0 : no hermitian or symmetric
            | 1 : hermitian
            | 2 : anti-hermitian

    Returns:
        Depending on the given dm, the function returns one J and one K matrix,
        or a list of J matrices and a list of K matrices, corresponding to the
        input density matrices.
    '''

    if np.iscomplexobj(dm) or np.iscomplexobj(eri):
        nao = dm.shape[-1]
        eri = eri.reshape((nao,)*4)
        def contract(dm):
            vj = np.einsum('ijkl,ji->kl', eri, dm)
            vk = np.einsum('ijkl,jk->il', eri, dm)
            return vj, vk
        if isinstance(dm, np.ndarray) and dm.ndim == 2:
            vj, vk = contract(dm)
        else:
            vjk = [contract(dmi) for dmi in dm]
            vj = lib.asarray([v[0] for v in vjk])
            vk = lib.asarray([v[1] for v in vjk])
    else:
        vj, vk = pyscf.scf.hf.dot_eri_dm(eri, dm, hermi)
    return vj, vk


class RHF(pyscf.scf.hf.RHF):
    '''RHF class adapted for PBCs.

    Attributes:
        kpt : (3,) ndarray
            The AO k-point in Cartesian coordinates, in units of 1/Bohr.
    '''
    def __init__(self, cell, kpt=np.zeros(3), exxdiv='ewald'):
        from pyscf.pbc.df import PWDF
        if not cell._built:
            sys.stderr.write('Warning: cell.build() is not called in input\n')
            cell.build()
        self.cell = cell
        pyscf.scf.hf.RHF.__init__(self, cell)

        self.exxdiv = exxdiv
        self.with_df = PWDF(cell)
        self.kpt = kpt
        self.direct_scf = False

        self._keys = self._keys.union(['cell', 'exxdiv', 'with_df'])

    @property
    def kpt(self):
        return self.with_df.kpts.reshape(3)
    @kpt.setter
    def kpt(self, x):
        self.with_df.kpts = np.reshape(x, (-1,3))

    def dump_flags(self):
        pyscf.scf.hf.RHF.dump_flags(self)
        logger.info(self, '******** PBC SCF flags ********')
        logger.info(self, 'kpt = %s', self.kpt)
        logger.info(self, 'DF object = %s', self.with_df)
        logger.info(self, 'Exchange divergence treatment (exxdiv) = %s', self.exxdiv)

    def get_hcore(self, cell=None, kpt=None):
        if cell is None: cell = self.cell
        if kpt is None: kpt = self.kpt
        if cell.pseudo is None:
            nuc = self.with_df.get_nuc(cell, kpt)
            return nuc + cell.pbc_intor('cint1e_kin_sph', 1, 1, kpt)
        else:
            return get_hcore(cell, kpt)

    def get_ovlp(self, cell=None, kpt=None):
        if cell is None: cell = self.cell
        if kpt is None: kpt = self.kpt
        return get_ovlp(cell, kpt)

    def get_jk(self, cell=None, dm=None, hermi=1, kpt=None, kpt_band=None):
        '''Get Coulomb (J) and exchange (K) following :func:`scf.hf.RHF.get_jk_`.

        Note the incore version, which initializes an _eri array in memory.
        '''
        if cell is None: cell = self.cell
        if dm is None: dm = self.make_rdm1()
        if kpt is None: kpt = self.kpt

        cpu0 = (time.clock(), time.time())
        dm = np.asarray(dm)
        nao = dm.shape[-1]

        if (kpt_band is None and
            (self.exxdiv == 'ewald' or self.exxdiv is None) and
            (self._eri is not None or cell.incore_anyway or self._is_mem_enough())):
            if self._eri is None:
                logger.debug(self, 'Building PBC AO integrals incore')
                self._eri = self.with_df.get_ao_eri(kpt, compact=True)
            vj, vk = dot_eri_dm(self._eri, dm.reshape(-1,nao,nao), hermi)

            if self.exxdiv == 'ewald':
                # G=0 is not inculded in the ._eri integrals
                vk = _ewald_exxdiv_for_G0(self, dm.reshape(-1,nao,nao), kpt, vk)
        else:
            vj, vk = self.with_df.get_jk(cell, dm, hermi, kpt, kpt_band, mf=self)

        logger.timer(self, 'vj and vk', *cpu0)
        return vj.reshape(dm.shape), vk.reshape(dm.shape)

    def get_j(self, cell=None, dm=None, hermi=1, kpt=None, kpt_band=None):
        '''Compute J matrix for the given density matrix.
        '''
        #return self.get_jk(cell, dm, hermi, kpt, kpt_band)[0]
        if cell is None: cell = self.cell
        if dm is None: dm = self.make_rdm1()
        if kpt is None: kpt = self.kpt

        cpu0 = (time.clock(), time.time())
        dm = np.asarray(dm)
        nao = dm.shape[-1]

        if (kpt_band is None and
            (self._eri is not None or cell.incore_anyway or self._is_mem_enough())):
            if self._eri is None:
                logger.debug(self, 'Building PBC AO integrals incore')
                self._eri = self.with_df.get_ao_eri(kpt, compact=True)
            vj, vk = dot_eri_dm(self._eri, dm.reshape(-1,nao,nao), hermi)
        else:
            vj = self.with_df.get_jk(cell, dm.reshape(-1,nao,nao), hermi,
                                     kpt, kpt_band, with_k=False)[0]
        logger.timer(self, 'vj', *cpu0)
        return vj.reshape(dm.shape)

    def get_k(self, cell=None, dm=None, hermi=1, kpt=None, kpt_band=None):
        '''Compute K matrix for the given density matrix.
        '''
        return self.get_jk(cell, dm, hermi, kpt, kpt_band)[1]

    def get_veff(self, cell=None, dm=None, dm_last=0, vhf_last=0, hermi=1,
                 kpt=None, kpt_band=None):
        '''Hartree-Fock potential matrix for the given density matrix.
        See :func:`scf.hf.get_veff` and :func:`scf.hf.RHF.get_veff`
        '''
        if cell is None: cell = self.cell
        if dm is None: dm = self.make_rdm1()
        if kpt is None: kpt = self.kpt
        vj, vk = self.get_jk(cell, dm, hermi, kpt, kpt_band)
        return vj - vk * .5

    def get_jk_incore(self, cell=None, dm=None, hermi=1, verbose=logger.DEBUG, kpt=None):
        '''Get Coulomb (J) and exchange (K) following :func:`scf.hf.RHF.get_jk_`.

        *Incore* version of Coulomb and exchange build only.
        Currently RHF always uses PBC AO integrals (unlike RKS), since
        exchange is currently computed by building PBC AO integrals.
        '''
        if cell is None: cell = self.cell
        if kpt is None: kpt = self.kpt
        if self._eri is None:
            self._eri = self.with_df.get_ao_eri(kpt, compact=True)
        return self.get_jk(cell, dm, hermi, verbose, kpt)

    def energy_tot(self, dm=None, h1e=None, vhf=None):
        etot = self.energy_elec(dm, h1e, vhf)[0] + self.cell.energy_nuc()
        return etot.real

    get_bands = get_bands

    def init_guess_by_chkfile(self, chk=None, project=True, kpt=None):
        if chk is None: chk = self.chkfile
        if kpt is None: kpt = self.kpt
        return init_guess_by_chkfile(self.cell, chk, project, kpt)
    def from_chk(self, chk=None, project=True, kpt=None):
        return self.init_guess_by_chkfile(chk, project, kpt)

    def dump_chk(self, envs):
        pyscf.scf.hf.RHF.dump_chk(self, envs)
        if self.chkfile:
            with h5py.File(self.chkfile) as fh5:
                fh5['scf/kpt'] = self.kpt
        return self

    def _is_mem_enough(self):
        nao = self.cell.nao_nr()
        if abs(self.kpt).sum() < 1e-9:
            mem_need = nao**4*8/4/1e6
        else:
            mem_need = nao**4*16/1e6
        return mem_need + lib.current_memory()[0] < self.max_memory*.95


def _ewald_exxdiv_for_G0(mf, dm, kpt, vk):
    cell = mf.cell
    gs = (0,0,0)
    Gv = np.zeros((1,3))
    ovlp = mf.get_ovlp(cell, kpt)
    coulGk = tools.get_coulG(cell, kpt-kpt, True, mf, gs, Gv)[0]
    logger.debug(mf, 'Total energy shift = -1/2 * Nelec*madelung/cell.vol = %.12g',
                 coulGk/cell.vol*cell.nelectron * -.5)
    if isinstance(dm, np.ndarray) and dm.ndim == 2:
        vk += coulGk/cell.vol * reduce(np.dot, (ovlp, dm, ovlp))
        nelec = np.einsum('ij,ij', ovlp, dm)
    else:
        nelec = 0
        for k, dmi in enumerate(dm):
            vk[k] += coulGk/cell.vol * reduce(np.dot, (ovlp, dmi, ovlp))
            nelec += np.einsum('ij,ij', ovlp, dmi)
    #if abs(nelec - cell.nelectron) > .1 and abs(nelec) > .1:
    #    logger.debug(mf, 'Tr(dm,S) = %g', nelec)
    #    sys.stderr.write('Warning: The input dm is not SCF density matrix. '
    #                     'The Ewald treatment on G=0 term for K matrix '
    #                     'might not be well defined.  Set mf.exxdiv=None '
    #                     'to switch off the Ewald term.\n')
    return vk

